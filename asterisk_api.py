#!/usr/bin/env python3
"""
Asterisk Recordings API Endpoints and Processing
"""
import os
import glob
import json
import shutil
import logging
import time
import requests
import subprocess
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any, Optional

from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel
import mysql.connector
from mysql.connector import Error
# Configure logging
logger = logging.getLogger(__name__)

# Configure Asterisk recordings directory
ASTERISK_RECORDINGS_DIR = os.environ.get("ASTERISK_RECORDINGS_DIR", "/var/spool/asterisk/monitor")

# Models
class PreprocessingOptions(BaseModel):
    noiseReduction: str = "medium"  # auto, light, medium, aggressive, none
    silenceRemoval: str = "medium"  # auto, light, medium, aggressive, none
    normalizeVolume: bool = True

class ImportAsteriskRequest(BaseModel):
    recordings: List[str]
    preprocessing: Optional[PreprocessingOptions] = None

class ImportAsteriskResponse(BaseModel):
    status: str
    message: str
    imported: int
    failed: int
    voice_name: str

class AsteriskRecording(BaseModel):
    id: str
    filename: str
    path: str
    date: Optional[str] = None
    callerid: Optional[str] = None
    duration: Optional[float] = None
    size: Optional[int] = None

class AsteriskRecordingsResponse(BaseModel):
    recordings: List[AsteriskRecording]
    count: int

# Create router
router = APIRouter(prefix="/api/asterisk", tags=["asterisk"])

# Helper functions
def scan_asterisk_recordings() -> List[AsteriskRecording]:
    """Scan Asterisk recordings directory for all recordings"""
    recordings = []
    
    # Get paths for all WAV and GSM files
    wav_files = glob.glob(os.path.join(ASTERISK_RECORDINGS_DIR, "**/*.wav"), recursive=True)
    gsm_files = glob.glob(os.path.join(ASTERISK_RECORDINGS_DIR, "**/*.gsm"), recursive=True)
    g729_files = glob.glob(os.path.join(ASTERISK_RECORDINGS_DIR, "**/*.g729"), recursive=True)
    
    all_files = wav_files + gsm_files + g729_files
    
    for file_path in all_files:
        try:
            file_stats = os.stat(file_path)
            path = Path(file_path)
            
            # Extract recording date from file timestamp
            date = datetime.fromtimestamp(file_stats.st_mtime).isoformat()
            
            # Generate a unique ID for this recording
            recording_id = f"{path.stem}_{int(file_stats.st_mtime)}"
            
            # Parse filename for additional metadata
            # Asterisk often names files like "exten-did-timestamp" or contains caller ID in the path
            filename = path.name
            callerid = None
            
            # Try to extract caller ID from filename or path
            # This is very dependent on your Asterisk setup
            path_parts = str(path).split(os.sep)
            for part in path_parts:
                if part.startswith("from-") or part.startswith("to-"):
                    callerid = part.split("-", 1)[1] if "-" in part else None
            
            # Get audio duration using ffprobe
            duration = None
            try:
                result = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", 
                    "default=noprint_wrappers=1:nokey=1", file_path],
                    capture_output=True, text=True
                )
                if result.returncode == 0:
                    duration = float(result.stdout.strip())
            except:
                logger.warning(f"Could not determine duration for {file_path}")
            
            recordings.append(AsteriskRecording(
                id=recording_id,
                filename=filename,
                path=str(file_path),
                date=date,
                callerid=callerid,
                duration=duration,
                size=file_stats.st_size
            ))
            
        except Exception as e:
            logger.error(f"Error processing recording {file_path}: {str(e)}")
    
    # Sort by date, most recent first
    recordings.sort(key=lambda x: x.date if x.date else "", reverse=True)
    
    return recordings

def get_recording_by_id(recording_id: str) -> Optional[AsteriskRecording]:
    """Get an Asterisk recording by ID"""
    recordings = scan_asterisk_recordings()
    for recording in recordings:
        if recording.id == recording_id:
            return recording
    return None

def preprocess_recording(source_path: str, target_path: str, options: PreprocessingOptions) -> bool:
    """
    Preprocess an Asterisk recording for voice cloning
    1. Convert to proper format (WAV, 22.05kHz, mono)
    2. Apply noise reduction
    3. Remove silence
    4. Normalize volume
    """
    try:
        # Determine noise reduction level in dB
        noise_reduction_level = {
            "light": "-10",         # Changed to negative
            "medium": "-20",        # Changed to negative
            "aggressive": "-30",    # Changed to negative
            "none": "0",
            "auto": "-20"           # Changed to negative
        }[options.noiseReduction]
        
        # Determine silence threshold in dB
        silence_threshold = {
            "light": "-50dB",
            "medium": "-40dB",
            "aggressive": "-30dB",
            "none": "-90dB",  # Effectively no removal
            "auto": "-40dB"  # Default to medium for auto
        }[options.silenceRemoval]
        
        # Create the ffmpeg command
        command = ["ffmpeg", "-y", "-i", source_path]
        
        # Apply noise reduction if enabled
        if options.noiseReduction != "none":
            command.extend([
                "-af", f"afftdn=nf={noise_reduction_level}"
            ])
        
        # Apply silence removal if enabled
        if options.silenceRemoval != "none":
            if "-af" in command:
                # Append to existing audio filter
                af_index = command.index("-af")
                command[af_index + 1] += f",silenceremove=start_periods=1:start_threshold={silence_threshold}:stop_periods=1:stop_threshold={silence_threshold}"
            else:
                # Add new audio filter
                command.extend([
                    "-af", f"silenceremove=start_periods=1:start_threshold={silence_threshold}:stop_periods=1:stop_threshold={silence_threshold}"
                ])
        
        # Apply normalization if enabled
        if options.normalizeVolume:
            if "-af" in command:
                # Append to existing audio filter
                af_index = command.index("-af")
                command[af_index + 1] += ",loudnorm=I=-16:TP=-1.5:LRA=11"
            else:
                # Add new audio filter
                command.extend([
                    "-af", "loudnorm=I=-16:TP=-1.5:LRA=11"
                ])
        
        # Set output parameters (always convert to proper format)
        command.extend([
            "-ac", "1",           # Mono
            "-ar", "22050",       # Sample rate 22.05kHz (optimal for TTS)
            "-c:a", "pcm_s16le",  # 16-bit PCM
            target_path
        ])
        
        # Run the command
        logger.info(f"Preprocessing command: {' '.join(command)}")
        result = subprocess.run(command, capture_output=True, text=True)
        
        if result.returncode != 0:
            logger.error(f"Preprocessing failed: {result.stderr}")
            return False
            
        return True
        
    except Exception as e:
        logger.error(f"Error preprocessing recording: {str(e)}")
        return False

def import_asterisk_recording(voice_name: str, recording_id: str, preprocessing_options: PreprocessingOptions) -> bool:
    """
    Import an Asterisk recording to a voice project
    1. Find the recording
    2. Preprocess it
    3. Save to the voice samples directory
    """
    # Get the recording
    recording = get_recording_by_id(recording_id)
    if not recording:
        logger.error(f"Recording not found: {recording_id}")
        return False
    
    try:
        # Get source path
        source_path = recording.path
        
        # Setup target paths
        voice_dir = Path(f"/opt/asterisk-tts-cloning/voice_samples/{voice_name}")
        samples_dir = voice_dir / "samples"
        
        # Create directories if they don't exist
        os.makedirs(samples_dir, exist_ok=True)
        
        # Generate a unique filename
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target_filename = f"asterisk_{Path(source_path).stem}_{timestamp}.wav"
        target_path = str(samples_dir / target_filename)
        
        # Preprocess the recording
        if not preprocess_recording(source_path, target_path, preprocessing_options):
            return False
        
        # Create metadata file
        metadata_path = samples_dir / f"{Path(target_filename).stem}_metadata.json"
        metadata = {
            "source": "asterisk",
            "original_file": recording.filename,
            "recording_id": recording_id,
            "callerid": recording.callerid,
            "date": recording.date,
            "duration": recording.duration,
            "preprocessing": preprocessing_options.dict(),
            "import_date": datetime.now().isoformat()
        }
        
        with open(metadata_path, "w") as f:
            json.dump(metadata, f, indent=2)
        
        logger.info(f"Successfully imported Asterisk recording {recording_id} to {target_path}")
        return True
        
    except Exception as e:
        logger.error(f"Error importing recording {recording_id}: {str(e)}")
        return False

# Endpoint handlers
@router.get("/recordings", response_model=AsteriskRecordingsResponse)
async def list_recordings():
    """List all Asterisk recordings"""
    recordings = scan_asterisk_recordings()
    return {"recordings": recordings, "count": len(recordings)}

@router.get("/recording/{recording_id}")
async def get_recording(recording_id: str):
    """Get a specific Asterisk recording"""
    recording = get_recording_by_id(recording_id)
    if not recording:
        raise HTTPException(status_code=404, detail="Recording not found")
    
    # Return the file
    return FileResponse(
        recording.path,
        media_type="audio/wav" if recording.path.endswith(".wav") else "audio/x-gsm",
        filename=recording.filename
    )

@router.post("/import-test")
async def test_import(recording_id: str, voice_name: str):
    """Test importing a recording to verify format compatibility"""
    recording = get_recording_by_id(recording_id)
    if not recording:
        raise HTTPException(status_code=404, detail="Recording not found")
    
    # Test the format using ffprobe
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,channels,sample_rate", 
             "-of", "json", recording.path],
            capture_output=True, text=True
        )
        
        if result.returncode != 0:
            return {"status": "error", "message": f"Could not analyze recording: {result.stderr}"}
        
        info = json.loads(result.stdout)
        
        return {
            "status": "success",
            "recording_id": recording_id,
            "voice_name": voice_name,
            "format_info": info,
            "can_convert": True  # ffmpeg should be able to convert most formats
        }
        
    except Exception as e:
        return {"status": "error", "message": f"Error analyzing recording: {str(e)}"}

@router.post("/voices/{voice_name}/import-asterisk", response_model=ImportAsteriskResponse)
async def import_asterisk_recordings(
    voice_name: str, 
    request: ImportAsteriskRequest, 
    background_tasks: BackgroundTasks
):
    """Import Asterisk recordings to a voice project"""
    # Check voice name
    voice_dir = Path(f"/opt/asterisk-tts-cloning/voice_samples/{voice_name}")
    os.makedirs(voice_dir, exist_ok=True)
    
    # Use default preprocessing options if not provided
    preprocessing_options = request.preprocessing or PreprocessingOptions()
    
    # Import each recording
    success_count = 0
    failed_count = 0
    
    for recording_id in request.recordings:
        if import_asterisk_recording(voice_name, recording_id, preprocessing_options):
            success_count += 1
        else:
            failed_count += 1
    
    # Optionally trigger voice processing in the background
    if success_count > 0:
        background_tasks.add_task(trigger_voice_processing, voice_name)
    
    return {
        "status": "success" if success_count > 0 else "error",
        "message": f"Imported {success_count} of {len(request.recordings)} recordings",
        "imported": success_count,
        "failed": failed_count,
        "voice_name": voice_name
    }

@router.get("/sip/agents")
async def get_sip_agents():
    """Get SIP peer agents from the MySQL database"""
    try:
        # Connect to MySQL
        connection = mysql.connector.connect(
            host="localhost",
            database="switchboard",
            user="root",  # Replace with your MySQL username
            password="zPFv6XIPyrvFTEAYwY"  # Replace with your MySQL password
        )
        
        if connection.is_connected():
            cursor = connection.cursor(dictionary=True)
            
            # Query for SIP extensions (non-trunks)
            cursor.execute("SELECT name, callerid FROM sippeers WHERE category != 'trunk' OR category IS NULL")
            extensions = cursor.fetchall()
            
            # Query for SIP trunks
            cursor.execute("SELECT name, callerid FROM sippeers WHERE category = 'trunk'")
            trunks = cursor.fetchall()
            
            # Format the results
            result = {
                "extensions": [{"name": ext["name"], "callerid": ext["callerid"]} for ext in extensions],
                "trunks": [{"name": trunk["name"], "callerid": trunk["callerid"]} for trunk in trunks]
            }
            
            cursor.close()
            connection.close()
            
            return result
    except Error as e:
        logger.error(f"Error accessing MySQL database: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    
    # Return empty lists if there's an issue
    return {"extensions": [], "trunks": []}
    
def trigger_voice_processing(voice_name: str):
    """Trigger voice processing after importing recordings"""
    try:
        # Wait a bit to ensure all files are saved
        time.sleep(2)
        
        # Call the process endpoint
        url = f"http://localhost:5002/api/voices/{voice_name}/process"
        requests.post(url)
        
        logger.info(f"Triggered voice processing for {voice_name}")
    except Exception as e:
        logger.error(f"Failed to trigger voice processing: {str(e)}")
        
# Add these endpoints to asterisk_api.py around line 280 (after existing endpoints)

@router.delete("/recording/{recording_id}")
async def delete_recording(recording_id: str):
    """Delete a specific Asterisk recording"""
    recording = get_recording_by_id(recording_id)
    if not recording:
        raise HTTPException(status_code=404, detail="Recording not found")
    
    try:
        # Delete the actual file
        if os.path.exists(recording.path):
            os.remove(recording.path)
            logger.info(f"Deleted recording file: {recording.path}")
        else:
            logger.warning(f"Recording file not found: {recording.path}")
        
        return {
            "status": "success",
            "message": f"Recording {recording.filename} deleted successfully"
        }
        
    except Exception as e:
        logger.error(f"Error deleting recording {recording_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to delete recording: {str(e)}")

@router.delete("/recordings/bulk")
async def delete_bulk_recordings(recording_ids: List[str]):
    """Delete multiple Asterisk recordings"""
    if not recording_ids:
        raise HTTPException(status_code=400, detail="No recording IDs provided")
    
    try:
        deleted_count = 0
        failed_count = 0
        failed_recordings = []
        
        for recording_id in recording_ids:
            try:
                recording = get_recording_by_id(recording_id)
                if not recording:
                    failed_count += 1
                    failed_recordings.append(f"{recording_id} (not found)")
                    continue
                
                # Delete the actual file
                if os.path.exists(recording.path):
                    os.remove(recording.path)
                    deleted_count += 1
                    logger.info(f"Deleted recording file: {recording.path}")
                else:
                    failed_count += 1
                    failed_recordings.append(f"{recording.filename} (file not found)")
                    
            except Exception as e:
                failed_count += 1
                failed_recordings.append(f"{recording_id} (error: {str(e)})")
                logger.error(f"Error deleting recording {recording_id}: {str(e)}")
        
        return {
            "status": "success" if deleted_count > 0 else "error",
            "message": f"Deleted {deleted_count} recordings, {failed_count} failed",
            "deleted_count": deleted_count,
            "failed_count": failed_count,
            "failed_recordings": failed_recordings
        }
        
    except Exception as e:
        logger.error(f"Error in bulk delete: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Bulk delete failed: {str(e)}")

@router.delete("/recordings/all")
async def delete_all_recordings():
    """Delete all Asterisk recordings (use with caution!)"""
    try:
        recordings = scan_asterisk_recordings()
        
        if not recordings:
            return {
                "status": "success",
                "message": "No recordings found to delete",
                "deleted_count": 0
            }
        
        deleted_count = 0
        failed_count = 0
        failed_recordings = []
        
        for recording in recordings:
            try:
                if os.path.exists(recording.path):
                    os.remove(recording.path)
                    deleted_count += 1
                    logger.info(f"Deleted recording file: {recording.path}")
                else:
                    failed_count += 1
                    failed_recordings.append(f"{recording.filename} (file not found)")
                    
            except Exception as e:
                failed_count += 1
                failed_recordings.append(f"{recording.filename} (error: {str(e)})")
                logger.error(f"Error deleting recording {recording.path}: {str(e)}")
        
        return {
            "status": "success" if deleted_count > 0 else "error",
            "message": f"Deleted {deleted_count} recordings, {failed_count} failed",
            "deleted_count": deleted_count,
            "failed_count": failed_count,
            "failed_recordings": failed_recordings
        }
        
    except Exception as e:
        logger.error(f"Error deleting all recordings: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to delete all recordings: {str(e)}")

