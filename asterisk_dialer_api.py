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
import random
import threading
import requests
from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import socket
import re

# Configure logging
logger = logging.getLogger(__name__)

# Configuration
ASTERISK_AMI_HOST = os.environ.get("ASTERISK_AMI_HOST", "localhost")
ASTERISK_AMI_PORT = int(os.environ.get("ASTERISK_AMI_PORT", "5038"))
ASTERISK_AMI_USER = os.environ.get("ASTERISK_AMI_USER", "admin")
ASTERISK_AMI_PASSWORD = os.environ.get("ASTERISK_AMI_PASSWORD", "7e56eb775e0baabc5b29012b22f36733")
ASTERISK_CONF_DIR = os.environ.get("ASTERISK_CONF_DIR", "/etc/asterisk")
ASTERISK_AGI_DIR = os.environ.get("ASTERISK_AGI_DIR", "/var/lib/asterisk/agi-bin")
ASTERISK_SOUNDS_DIR = os.environ.get("ASTERISK_SOUNDS_DIR", "/var/lib/asterisk/sounds/custom")
ASTERISK_SOUNDS_DIR_TTS = os.environ.get("ASTERISK_SOUNDS_DIR", "/var/lib/asterisk/sounds/custom/tts_files")
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
    sip_trunk: str
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
    """Initiate a call using Asterisk AMI directly"""
    ami_sock = None
    try:
        # Define conference room number (using random 6-digit number)
        conf_room = ''.join(random.choices('0123456789', k=6))
        
        # Connect to AMI
        ami_sock = ami_connect()
        if not ami_sock:
            raise Exception("Failed to connect to AMI")
        
        # Get caller ID (use a default if not specified)
        caller_id = call_record.get('caller_id')
        caller_id_name = caller_id if caller_id else "TTS Call"
        caller_id_num = caller_id if caller_id else "0000000000"
        
        # Originate agent leg
        agent_params = {
            "Channel": f"SIP/{call_record['sip_name']}",
            "Application": "ConfBridge",
            "Data": conf_room,
            "CallerID": f"\"{caller_id_name}\" <{caller_id_num}>",
            "Timeout": "30000",  # 30 seconds in milliseconds
            "Async": "true"      # Don't wait for answer
        }
        
        logger.info(f"Originating agent leg: {agent_params}")
        agent_response = ami_send_action(ami_sock, "Originate", agent_params)
        logger.info(f"Agent originate response: {agent_response}")
        
        # Extract ActionID for tracking
        agent_action_id = None
        match = re.search(r'ActionID:\s*(\S+)', agent_response or '')
        if match:
            agent_action_id = match.group(1)
        
        # Update call record with conference info and action ID
        call_record['conference_room'] = conf_room
        call_record['agent_action_id'] = agent_action_id
        
        # Wait a moment for the agent leg to be established
        time.sleep(2)
        
        # Now dial the customer into the same conference
        customer_params = {
            "Channel": f"SIP/{call_record['sip_trunk']}/{call_record['destination']}",
            "Application": "ConfBridge",
            "Data": conf_room,
            "CallerID": f"\"{caller_id_name}\" <{caller_id_num}>",
            "Timeout": "30000",  # 30 seconds in milliseconds
            "Async": "true"      # Don't wait for answer
        }
        
        logger.info(f"Originating customer leg: {customer_params}")
        customer_response = ami_send_action(ami_sock, "Originate", customer_params)
        logger.info(f"Customer originate response: {customer_response}")
        
        # Extract ActionID for tracking
        customer_action_id = None
        match = re.search(r'ActionID:\s*(\S+)', customer_response or '')
        if match:
            customer_action_id = match.group(1)
        
        # Update call record with customer action ID
        call_record['customer_action_id'] = customer_action_id
        
        # Start a background task to monitor call status
        threading.Thread(target=monitor_call_status, args=(call_record['id'], conf_room), daemon=True).start()
        
        # Update call status to "dialing"
        return update_call_status(call_record['id'], "dialing", {
            "conference_room": conf_room,
            "start_time": datetime.now().isoformat(),
            "agent_action_id": agent_action_id,
            "customer_action_id": customer_action_id
        })
        
    except Exception as e:
        logger.error(f"Error initiating call: {e}")
        # Update call status to "failed"
        update_call_status(call_record['id'], "failed", {
            "error": str(e)
        })
        return None
    finally:
        # Close AMI connection
        ami_close(ami_sock)

def monitor_call_status(call_id, conf_room):
    """Monitor the status of a call and update when it's answered using AMI"""
    ami_sock = None
    try:
        # Wait a short time to allow call setup
        time.sleep(5)
        
        # Get call record to have latest info
        call_record = get_call_by_id(call_id)
        if not call_record:
            logger.error(f"Call record {call_id} not found")
            return
        
        # Connect to AMI
        ami_sock = ami_connect()
        if not ami_sock:
            raise Exception("Failed to connect to AMI")
        
        # Maximum time to wait for call to be answered (30 seconds)
        max_wait_time = 30
        wait_interval = 2
        total_waited = 0
        
        # Store channels we've seen as connected
        connected_channels = set()
        
        while total_waited < max_wait_time:
            # Get all active channels for better diagnostics
            all_channels_response = ami_send_action(ami_sock, "CoreShowChannels")
            active_channels = []
            for line in all_channels_response.splitlines():
                if line.startswith("Channel:"):
                    channel = line.split(":")[1].strip()
                    active_channels.append(channel)
            
            logger.info(f"Found {len(active_channels)} active channels")
            
            # Get specific SIP/channel state if possible
            sip_name = call_record.get('sip_name', '')
            destination = call_record.get('destination', '')
            
            # Look for SIP extension and destination channels
            sip_channel = next((c for c in active_channels if sip_name in c), None)
            dest_channel = next((c for c in active_channels if destination in c), None)
            
            if sip_channel or dest_channel:
                connected_channels.update([c for c in [sip_channel, dest_channel] if c])
                
                # If we've seen both channels, consider call connected
                if len(connected_channels) >= 2:
                    update_call_status(call_id, "connected")
                    logger.info(f"Call {call_id} is now connected with channels: {connected_channels}")
                    return
            
            # Also check conference participants
            list_params = {
                "Conference": conf_room
            }
            
            list_response = ami_send_action(ami_sock, "ConfbridgeList", list_params)
            participant_count = list_response.count("Channel:")
            
            logger.info(f"Found {participant_count} participants in conference {conf_room}")
            
            # Extract channel names from conference
            conf_channels = []
            for line in list_response.splitlines():
                if line.startswith("Channel:"):
                    channel = line.split(":")[1].strip()
                    conf_channels.append(channel)
                    connected_channels.add(channel)
            
            if conf_channels:
                logger.info(f"Conference channels: {conf_channels}")
            
            if participant_count >= 2 or len(connected_channels) >= 2:
                # Both participants are in the conference, call is connected
                update_call_status(call_id, "connected", {
                    "channels": list(connected_channels)  # Store for future reference
                })
                logger.info(f"Call {call_id} is now connected with {participant_count} participants")
                return
            elif (participant_count == 1 or len(connected_channels) == 1) and total_waited >= 10:
                # Only one participant after 10 seconds, mark as connected anyway
                # This handles the case where only one leg answers
                update_call_status(call_id, "connected", {
                    "channels": list(connected_channels)  # Store for future reference
                })
                logger.info(f"Call {call_id} is partially connected with {len(connected_channels)} participant")
                return
            
            # Try CLI method as a backup
            try:
                check_cmd = [
                    "asterisk", "-rx",
                    f"confbridge list {conf_room}"
                ]
                
                result = subprocess.run(check_cmd, capture_output=True, text=True)
                cli_output = result.stdout
                logger.info(f"ConfBridge CLI output: {cli_output}")
                
                if "No active conferences" not in cli_output and len(cli_output.strip()) > 0:
                    # Count lines that might indicate participants
                    lines = cli_output.strip().split("\n")
                    cli_participants = 0
                    
                    for line in lines:
                        if "Channel" in line and "User Profile" in line:
                            # This is the header line, skip it
                            continue
                        if line.strip() and not line.startswith("No") and "===" not in line:
                            cli_participants += 1
                            # Try to extract channel name
                            parts = line.strip().split()
                            if parts and (parts[0].startswith("SIP/") or parts[0].startswith("Local/")):
                                connected_channels.add(parts[0])
                    
                    logger.info(f"CLI shows {cli_participants} participants")
                    if cli_participants >= 2 or len(connected_channels) >= 2:
                        update_call_status(call_id, "connected", {
                            "channels": list(connected_channels)  # Store for future reference
                        })
                        logger.info(f"Call {call_id} is now connected (detected via CLI)")
                        return
                    elif cli_participants == 1 and total_waited >= 10:
                        update_call_status(call_id, "connected", {
                            "channels": list(connected_channels)  # Store for future reference
                        })
                        logger.info(f"Call {call_id} is partially connected (detected via CLI)")
                        return
            except Exception as cli_e:
                logger.warning(f"Error checking via CLI: {cli_e}")
            
            # Sleep before checking again
            time.sleep(wait_interval)
            total_waited += wait_interval
        
        # Final check at the end of the timeout period
        update_call_status(call_id, "connected", {
            "channels": list(connected_channels)  # Store any channels we've seen
        })
        logger.info(f"Call {call_id} marked as connected after timeout")
        
    except Exception as e:
        logger.error(f"Error monitoring call status: {e}")
        # Mark as connected anyway to allow playing TTS
        update_call_status(call_id, "connected", {
            "error_msg": str(e)
        })
        logger.info(f"Call {call_id} marked as connected despite error")
    finally:
        # Close AMI connection
        ami_close(ami_sock)
        
def play_tts_in_conference(call_record):
    """Play TTS in a conference bridge using AMI with enhanced error handling"""
    ami_sock = None
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
        
        # Connect to AMI
        ami_sock = ami_connect()
        if not ami_sock:
            raise Exception("Failed to connect to AMI")
        
        # Get all active channels first to see what's available
        logger.info("Getting list of all active channels")
        all_channels_response = ami_send_action(ami_sock, "CoreShowChannels")
        logger.info(f"All active channels: {all_channels_response}")
        
        # Get all active conferences for additional diagnostics
        logger.info("Getting list of all active conferences")
        all_conf_response = ami_send_action(ami_sock, "ConfbridgeListRooms")
        logger.info(f"All active conferences: {all_conf_response}")
        
        # Get the participants in the conference
        list_params = {
            "Conference": conf_room
        }
        
        list_response = ami_send_action(ami_sock, "ConfbridgeList", list_params)
        logger.info(f"ConfbridgeList response for room {conf_room}: {list_response}")
        
        # Extract channels from the response
        channels = []
        for line in list_response.splitlines():
            if line.startswith("Channel:"):
                channel = line.split(":")[1].strip()
                channels.append(channel)
        
        # If we didn't find any channels, try looking for active channels in general
        if not channels:
            logger.warning(f"No channels found in conference {conf_room}, trying to find them from active channels")
            
            # Look for any channel that might be part of this call (based on call ID)
            active_channels = []
            for line in all_channels_response.splitlines():
                if line.startswith("Channel:"):
                    channel = line.split(":")[1].strip()
                    active_channels.append(channel)
            
            logger.info(f"Found {len(active_channels)} active channels overall")
            
            # Also try to check conference participants in a more direct way
            try:
                check_cmd = [
                    "asterisk", "-rx",
                    f"confbridge list {conf_room}"
                ]
                
                result = subprocess.run(check_cmd, capture_output=True, text=True)
                logger.info(f"Direct CLI check for conference {conf_room}: {result.stdout}")
                
                # Extract channels from CLI output
                for line in result.stdout.splitlines():
                    if line.startswith("SIP/") or line.startswith("Local/"):
                        channel_name = line.split()[0].strip()
                        if channel_name and channel_name not in channels:
                            channels.append(channel_name)
            except Exception as cli_e:
                logger.warning(f"Error checking conference via CLI: {cli_e}")
        
        # If still no channels, try a more aggressive approach
        if not channels and active_channels:
            logger.warning("Still no channels found, trying more aggressive methods")
            
            # Get the call_id
            call_id = call_record.get('id')
            
            # Try to find any relevant channel
            for channel in active_channels:
                # Use educated guesses based on SIP name and destination
                sip_name = call_record.get('sip_name', '').replace('SIP/', '')
                destination = call_record.get('destination', '')
                
                if (sip_name and sip_name in channel) or (destination and destination in channel):
                    logger.info(f"Found potential matching channel: {channel}")
                    channels.append(channel)
        
        if not channels:
            logger.error(f"No channels found in conference {conf_room} and no matching active channels")
            
            # Try one more method - play to all active channels as a last resort
            if active_channels and len(active_channels) <= 5:  # Limit to 5 channels to avoid spamming
                logger.warning(f"As a last resort, trying to play to all active channels: {active_channels}")
                channels = active_channels
            else:
                raise Exception(f"No channels found in conference {conf_room}")
        
        logger.info(f"Will attempt to play to channels: {channels}")
        
        # Remove file extension for Asterisk playback (Asterisk will add it back)
        file_base = os.path.splitext(tts_file)[0]
        
        # Try multiple playback methods
        success = False
        
        # Method 2: Try direct channel playback
        if channels and not success:
            for channel in channels:
                try:
                    logger.info(f"Trying playback to channel {channel}")
                    announce_channel = f"{channel},qB"
                    # Method 2a: Using Originate with Playback application
                    playback_params = {
                        'action': 'Originate',
                        'channel': 'Local/playback@play-to-conference',
                        'application': 'confbridge',
                        'data': conf_room,
                        'variable': f"ANNOUCE_CHANNEL={channel},AUDIO_PATH={file_base},CONF_NUM={conf_room}",
                        'async': "false"

                    }
                    
                    print(f"playback params {playback_params}")
                    channel_resp = ami_send_action(ami_sock, "Originate", playback_params)
                    logger.info(f"Playback to channel {channel} response: {channel_resp}")
                    
                    if "Success" in channel_resp:
                        logger.info(f"Playback to channel {channel} successful")
                        success = True
                        break
                
                except Exception as e2:
                    logger.warning(f"Playback to channel {channel} failed: {e2}")
        
        # Method 3: Use CLI-based approach as a fallback
        if not success:
            logger.warning("AMI methods failed, trying CLI approach")
            try:
                for channel in channels:
                    cmd = [
                        "asterisk", "-rx",
                        f"channel playback {channel} {file_base}"
                    ]
                    
                    logger.info(f"Running CLI command: {' '.join(cmd)}")
                    result = subprocess.run(cmd, capture_output=True, text=True)
                    logger.info(f"CLI result: {result.stdout}")
                    
                    if "failed" not in result.stdout.lower() and "error" not in result.stdout.lower():
                        logger.info(f"CLI playback to {channel} seems successful")
                        success = True
                        break
            except Exception as e3:
                logger.warning(f"CLI playback approach failed: {e3}")
        
        if not success:
            raise Exception("All playback methods failed")
        
        return success
        
    except Exception as e:
        logger.error(f"Error playing TTS in conference: {e}")
        return False
    finally:
        # Close AMI connection
        ami_close(ami_sock)

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
                
                tts_file_path = tts_metadata.get('gsm_path') or tts_metadata.get('wav_path')
                
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
            "sip_trunk": call_request.sip_trunk,
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
    
def ami_connect(host="localhost", port=5038, username="admin", password="7e56eb775e0baabc5b29012b22f36733"):
    """Connect to Asterisk Manager Interface (AMI)"""
    try:
        # Create socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((host, port))
        
        # Read welcome message
        response = s.recv(1024).decode('utf-8')
        if not response.startswith('Asterisk'):
            raise Exception(f"Unexpected welcome message: {response}")
        
        # Login
        auth_cmd = f"Action: Login\r\nUsername: {username}\r\nSecret: {password}\r\n\r\n"
        s.send(auth_cmd.encode('utf-8'))
        
        # Read response
        response = s.recv(1024).decode('utf-8')
        if "Success" not in response:
            raise Exception(f"AMI login failed: {response}")
        
        return s
    except Exception as e:
        logger.error(f"AMI connection error: {e}")
        return None

def ami_send_action(sock, action, parameters=None):
    """Send an action to AMI and get the response"""
    try:
        # Build command
        cmd = f"Action: {action}\r\n"
        
        # Add parameters
        if parameters:
            for key, value in parameters.items():
                cmd += f"{key}: {value}\r\n"
        
        # End command
        cmd += "\r\n"
        
        # Send command
        sock.send(cmd.encode('utf-8'))
        
        # Read response - accumulate until we get a blank line
        response = ""
        buffer = sock.recv(1024).decode('utf-8')
        
        while buffer:
            response += buffer
            
            # Check if we've reached the end of the response
            if "\r\n\r\n" in response:
                break
                
            # Read more if needed
            try:
                sock.settimeout(0.5)  # Short timeout for additional data
                buffer = sock.recv(1024).decode('utf-8')
            except socket.timeout:
                break
        
        # Reset timeout
        sock.settimeout(None)
        
        return response
    except Exception as e:
        logger.error(f"AMI send action error: {e}")
        return None

def ami_close(sock):
    """Close AMI connection"""
    try:
        if sock:
            # Send logout
            sock.send(b"Action: Logoff\r\n\r\n")
            sock.close()
    except:
        pass