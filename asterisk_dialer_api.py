#!/usr/bin/env python3
"""
Asterisk Dialer API Endpoints
Handles call initiation, TTS playback, and integration with voice cloning
"""
import os
import json
import time
import uuid
import logging
import tempfile
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

import requests
from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# Configure logging
logger = logging.getLogger(__name__)

# Configuration
ASTERISK_AMI_HOST = os.environ.get("ASTERISK_AMI_HOST", "localhost")
ASTERISK_AMI_PORT = int(os.environ.get("ASTERISK_AMI_PORT", "5038"))
ASTERISK_AMI_USER = os.environ.get("ASTERISK_AMI_USER", "admin")
ASTERISK_AMI_PASSWORD = os.environ.get("ASTERISK_AMI_PASSWORD", "amp111")
ASTERISK_CONF_DIR = os.environ.get("ASTERISK_CONF_DIR", "/etc/asterisk")
ASTERISK_AGI_DIR = os.environ.get("ASTERISK_AGI_DIR", "/var/lib/asterisk/agi-bin")
ASTERISK_SOUNDS_DIR = os.environ.get("ASTERISK_SOUNDS_DIR", "/var/lib/asterisk/sounds/custom")
CALL_RECORDS_FILE = os.environ.get("CALL_RECORDS_FILE", "/opt/asterisk-tts-cloning/call_records.json")
TTS_API_URL = os.environ.get("TTS_API_URL", "http://localhost:8000/api/synthesize")

# Create necessary directories
os.makedirs(ASTERISK_SOUNDS_DIR, exist_ok=True)
os.makedirs(os.path.dirname(CALL_RECORDS_FILE), exist_ok=True)

# Models
class CallRequest(BaseModel):
    caller_id: Optional[str] = None
    destination: str
    sip_name: str
    message: str
    voice: str
    tts_file_id: Optional[str] = None  # Added field for pre-generated TTS file ID

class CallResponse(BaseModel):
    status: str
    message: str
    call_id: Optional[str] = None
    call: Optional[Dict[str, Any]] = None

class CallStatusResponse(BaseModel):
    status: str
    call: Dict[str, Any]

class CallsListResponse(BaseModel):
    calls: List[Dict[str, Any]]

class PlayTTSResponse(BaseModel):
    status: str
    message: str
    
class HangupResponse(BaseModel):
    status: str
    message: str

# Create router
router = APIRouter(prefix="/api/calls", tags=["calls"])

# Helper functions
def generate_call_id():
    """Generate a unique call ID"""
    return str(uuid.uuid4())

def load_call_records():
    """Load call records from file"""
    if not os.path.exists(CALL_RECORDS_FILE):
        return []
    
    try:
        with open(CALL_RECORDS_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading call records: {e}")
        return []

def save_call_records(records):
    """Save call records to file"""
    try:
        with open(CALL_RECORDS_FILE, 'w') as f:
            json.dump(records, f, indent=2)
        return True
    except Exception as e:
        logger.error(f"Error saving call records: {e}")
        return False

def get_call_by_id(call_id):
    """Get a call record by ID"""
    records = load_call_records()
    for record in records:
        if record.get('id') == call_id:
            return record
    return None

def update_call_status(call_id, status, additional_data=None):
    """Update the status of a call record"""
    records = load_call_records()
    for record in records:
        if record.get('id') == call_id:
            record['status'] = status
            record['updated_at'] = datetime.now().isoformat()
            
            if additional_data:
                for key, value in additional_data.items():
                    record[key] = value
            
            save_call_records(records)
            return record
    return None

def generate_tts_audio(text, voice, output_file):
    """Generate TTS audio and save to a file"""
    try:
        # Call the TTS API
        response = requests.post(
            TTS_API_URL,
            json={"text": text, "voice": voice},
            timeout=30
        )
        
        if response.status_code != 200:
            raise Exception(f"TTS API returned status code {response.status_code}")
        
        # Save the audio to a file
        with open(output_file, 'wb') as f:
            f.write(response.content)
        
        # Convert to Asterisk format if needed
        convert_cmd = [
            "sox",
            output_file,
            "-r", "8000",      # Sample rate
            "-c", "1",         # Mono
            "-b", "16",        # Bit depth
            f"{output_file}.gsm"
        ]
        
        subprocess.run(convert_cmd, check=True)
        
        return f"{output_file}.gsm"
    except Exception as e:
        logger.error(f"Error generating TTS audio: {e}")
        return None

def initiate_call_asterisk(call_record):
    """Initiate a call using Asterisk AMI"""
    try:
        # Define conference room number (using call_id last 6 digits)
        conf_room = call_record['id'][-6:]
        
        # Prepare Asterisk Manager Interface (AMI) command
        # We use a simple command-line utility to avoid dependencies
        originate_cmd = [
            "asterisk", "-rx",
            f"""
            channel originate Local/{call_record['sip_name']}@from-internal extension {conf_room}@conference-bridge
            """
        ]
        
        # Execute the command
        subprocess.Popen(originate_cmd)
        
        # Update call record with conference info
        call_record['conference_room'] = conf_room
        
        # Wait a moment for the agent leg to be established
        time.sleep(2)
        
        # Now dial the customer into the same conference
        customer_cmd = [
            "asterisk", "-rx",
            f"""
            channel originate Local/{call_record['destination']}@from-internal extension {conf_room}@conference-bridge
            """
        ]
        
        # Execute the command
        subprocess.Popen(customer_cmd)
        
        # Update call status to "dialing"
        return update_call_status(call_record['id'], "dialing", {
            "conference_room": conf_room,
            "start_time": datetime.now().isoformat()
        })
    except Exception as e:
        logger.error(f"Error initiating call: {e}")
        # Update call status to "failed"
        update_call_status(call_record['id'], "failed", {
            "error": str(e)
        })
        return None

def play_tts_in_conference(call_record):
    """Play TTS in a conference bridge"""
    try:
        # Get conference room
        conf_room = call_record.get('conference_room')
        if not conf_room:
            raise Exception("No conference room specified in call record")
        
        # Get TTS file
        tts_file = call_record.get('tts_file')
        if not tts_file:
            raise Exception("No TTS file specified in call record")
        
        # Check if file exists
        if not os.path.exists(tts_file):
            raise Exception(f"TTS file not found: {tts_file}")
        
        # Play the file in the conference
        play_cmd = [
            "asterisk", "-rx",
            f"confbridge play {conf_room} {tts_file}"
        ]
        
        # Execute the command
        subprocess.run(play_cmd, check=True)
        
        return True
    except Exception as e:
        logger.error(f"Error playing TTS in conference: {e}")
        return False

def hangup_call(call_record):
    """Hangup a call using Asterisk AMI"""
    try:
        # Get conference room
        conf_room = call_record.get('conference_room')
        if not conf_room:
            raise Exception("No conference room specified in call record")
        
        # Kick all participants from the conference
        hangup_cmd = [
            "asterisk", "-rx",
            f"confbridge kick {conf_room} all"
        ]
        
        # Execute the command
        subprocess.run(hangup_cmd, check=True)
        
        # Update call status to "completed"
        return update_call_status(call_record['id'], "completed", {
            "end_time": datetime.now().isoformat()
        })
    except Exception as e:
        logger.error(f"Error hanging up call: {e}")
        return None

# Endpoint handlers
@router.post("/initiate", response_model=CallResponse)
async def initiate_call(call_request: CallRequest, background_tasks: BackgroundTasks):
    """Initiate a new call with TTS capabilities"""
    try:
        # Generate call ID
        call_id = generate_call_id()
        
        # Determine TTS file to use
        tts_file_path = None
        
        if call_request.tts_file_id:
            # Use pre-generated TTS file
            logger.info(f"Using pre-generated TTS file with ID: {call_request.tts_file_id}")
            metadata_path = os.path.join(ASTERISK_SOUNDS_DIR, f"tts-{call_request.tts_file_id}.json")
            
            if os.path.exists(metadata_path):
                with open(metadata_path, 'r') as f:
                    tts_metadata = json.load(f)
                
                tts_file_path = tts_metadata.get('gsm_path')
                
                if not os.path.exists(tts_file_path):
                    logger.warning(f"Pre-generated TTS file not found: {tts_file_path}")
                    tts_file_path = None
            else:
                logger.warning(f"TTS metadata file not found: {metadata_path}")
        
        # If no pre-generated file found, generate a new one
        if not tts_file_path:
            logger.info(f"Generating new TTS file for call {call_id}")
            
            # Create a temporary file for TTS audio
            with tempfile.NamedTemporaryFile(delete=False, suffix='.wav', dir=ASTERISK_SOUNDS_DIR) as temp:
                tts_file_path = temp.name
            
            # Generate TTS audio
            tts_gsm_path = generate_tts_audio(call_request.message, call_request.voice, tts_file_path)
            
            if not tts_gsm_path:
                return JSONResponse(
                    status_code=500,
                    content={"status": "error", "message": "Failed to generate TTS audio"}
                )
                
            tts_file_path = tts_gsm_path
        
        # Create call record
        call_record = {
            "id": call_id,
            "caller_id": call_request.caller_id,
            "destination": call_request.destination,
            "sip_name": call_request.sip_name,
            "message": call_request.message,
            "voice": call_request.voice,
            "status": "initiated",
            "tts_file": tts_file_path,
            "tts_file_id": call_request.tts_file_id,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat()
        }
        
        # Save call record
        records = load_call_records()
        records.append(call_record)
        save_call_records(records)
        
        # Initiate the call in the background
        background_tasks.add_task(initiate_call_asterisk, call_record)
        
        return {
            "status": "success",
            "message": "Call initiated successfully",
            "call_id": call_id,
            "call": call_record
        }
    except Exception as e:
        logger.error(f"Error initiating call: {e}")
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": str(e)}
        )

@router.get("/recent", response_model=CallsListResponse)
async def get_recent_calls(limit: int = 10):
    """Get a list of recent calls"""
    try:
        records = load_call_records()
        
        # Sort by created_at field in descending order
        records.sort(key=lambda x: x.get('created_at', ''), reverse=True)
        
        # Return only the requested number of records
        return {
            "calls": records[:limit]
        }
    except Exception as e:
        logger.error(f"Error retrieving recent calls: {e}")
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": str(e)}
        )

@router.get("/{call_id}", response_model=CallStatusResponse)
async def get_call(call_id: str):
    """Get a specific call by ID"""
    call_record = get_call_by_id(call_id)
    if not call_record:
        raise HTTPException(status_code=404, detail="Call not found")
    
    return {
        "status": "success",
        "call": call_record
    }

@router.get("/{call_id}/status", response_model=CallStatusResponse)
async def get_call_status(call_id: str):
    """Get the status of a specific call"""
    call_record = get_call_by_id(call_id)
    if not call_record:
        raise HTTPException(status_code=404, detail="Call not found")
    
    # Check if call is in progress, use Asterisk AMI to get real-time status
    # This is a simplified implementation - in a real system, you would use AMI
    # to check the actual state of the conference bridge
    
    # For now, we'll just return the current record
    return {
        "status": "success",
        "call": call_record
    }

@router.post("/{call_id}/play-tts", response_model=PlayTTSResponse)
async def play_tts(call_id: str, background_tasks: BackgroundTasks):
    """Play TTS in an active call"""
    call_record = get_call_by_id(call_id)
    if not call_record:
        raise HTTPException(status_code=404, detail="Call not found")
    
    # Check if call is connected
    if call_record.get('status') != "connected" and call_record.get('status') != "dialing":
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "Call is not active"}
        )
    
    # Play TTS in the background
    background_tasks.add_task(play_tts_in_conference, call_record)
    
    return {
        "status": "success",
        "message": "TTS playback initiated"
    }

@router.post("/{call_id}/hangup", response_model=HangupResponse)
async def hangup(call_id: str, background_tasks: BackgroundTasks):
    """Hang up an active call"""
    call_record = get_call_by_id(call_id)
    if not call_record:
        raise HTTPException(status_code=404, detail="Call not found")
    
    # Check if call is still active
    if call_record.get('status') in ["completed", "failed"]:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "Call is already ended"}
        )
    
    # Hang up the call in the background
    background_tasks.add_task(hangup_call, call_record)
    
    return {
        "status": "success",
        "message": "Call hangup initiated"
    }