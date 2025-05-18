#!/usr/bin/env python3
"""
FastAPI server for managing voice cloning system
Handles voice creation, processing, activation, and status
"""
import os
import json
import tempfile
import logging
import shutil
import subprocess
import asyncio
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime

from fastapi import FastAPI, HTTPException, File, UploadFile, BackgroundTasks
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Voice Cloning API")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration
VOICES_DIR = "/opt/asterisk-tts-cloning/voice_samples"
PROCESSING_DIR = "/opt/asterisk-tts-cloning/processing"
ACTIVE_VOICE_FILE = "/opt/asterisk-tts-cloning/active_voice.json"
CONTAINER_NAME = "coqui-tts"

# Ensure directories exist
os.makedirs(VOICES_DIR, exist_ok=True)
os.makedirs(PROCESSING_DIR, exist_ok=True)

# API Configuration
class Config:
    VOICES_DIR = "/opt/asterisk-tts-cloning/voice_samples"
    ACTIVE_VOICE_FILE = "/opt/asterisk-tts-cloning/active_voice.json"
    TTS_SERVICE_URL = "http://localhost:5002/api/tts"
    TTS_CONTAINER_NAME = "coqui-tts"
    TEMP_DIR = "/tmp/tts-api"
    GENERATED_AUDIO_DIR = "/app/voice_samples/{voice_name}/generated"  # Template path
    
    @classmethod
    def get_generated_dir(cls, voice_name: str) -> str:
        """Get the generated audio directory for a voice"""
        return cls.GENERATED_AUDIO_DIR.format(voice_name=voice_name)
        
    @classmethod
    def ensure_dirs(cls):
        """Ensure necessary directories exist"""
        os.makedirs(cls.VOICES_DIR, exist_ok=True)
        os.makedirs(cls.TEMP_DIR, exist_ok=True)

Config.ensure_dirs()

# Models
class TTSRequest(BaseModel):
    text: str
    voice: Optional[str] = None
    language_variant: Optional[str] = None

class VoiceInfo(BaseModel):
    name: str
    status: str
    samples: int
    processed: int
    
class VoiceStatus(BaseModel):
    name: str
    status: str
    samples: int
    processed: int

class SystemHealth(BaseModel):
    status: str
    activeVoice: Optional[str]
    processing: int

class CreateVoiceResponse(BaseModel):
    status: str
    message: str

class HistoryItem(BaseModel):
    id: str
    text: str
    filename: str
    date: str
 
class VoiceSample(BaseModel):
    filename: str
    created_at: str
    metadata: Optional[Dict[str, Any]] = None

class SamplesResponse(BaseModel):
    samples: List[VoiceSample]
    count: int
    
# Helper functions
def get_active_voice() -> Optional[str]:
    """Get currently active voice"""
    try:
        if os.path.exists(ACTIVE_VOICE_FILE):
            with open(ACTIVE_VOICE_FILE, 'r') as f:
                data = json.load(f)
                return data.get('active_voice')
    except:
        pass
    return None

def set_active_voice(voice_name: Optional[str]) -> None:
    """Set active voice"""
    with open(ACTIVE_VOICE_FILE, 'w') as f:
        json.dump({'active_voice': voice_name, 'updated_at': str(datetime.now())}, f)

def get_voice_status(voice_name: str) -> str:
    """Get status of a voice"""
    voice_dir = Path(VOICES_DIR) / voice_name
    if not voice_dir.exists():
        return "error"
    
    samples_dir = voice_dir / "samples"
    processed_dir = voice_dir / "processed"
    status_file = voice_dir / "status.json"
    
    # Check for error status
    if status_file.exists():
        try:
            with open(status_file, 'r') as f:
                status_data = json.load(f)
                if status_data.get('status') == 'error':
                    return "error"
        except:
            pass
    
    has_samples = samples_dir.exists() and len(list(samples_dir.glob("*.wav"))) > 0
    has_processed = processed_dir.exists() and len(list(processed_dir.glob("*.wav"))) > 0
    
    if voice_name == get_active_voice():
        return "active"
    elif has_processed:
        return "ready"
    elif has_samples:
        # Check if processing just started (processed dir exists but empty)
        if processed_dir.exists() and len(list(processed_dir.glob("*.wav"))) == 0:
            return "processing"
        return "new"
    else:
        return "new"

def count_files(directory: Path, extension: str = "*.wav") -> int:
    """Count files in directory"""
    if not directory.exists():
        return 0
    return len(list(directory.glob(extension)))

def is_processing() -> int:
    """Check how many voices are being processed"""
    count = 0
    for voice_dir in Path(VOICES_DIR).iterdir():
        if voice_dir.is_dir() and get_voice_status(voice_dir.name) == "processing":
            count += 1
    return count

# API endpoints
@app.get("/api/health", response_model=SystemHealth)
async def health_check():
    """Check system health and status"""
    return SystemHealth(
        status="healthy",
        activeVoice=get_active_voice(),
        processing=is_processing()
    )

@app.get("/api/voices")
async def list_voices():
    """List all voices and their status"""
    voices = []
    
    for voice_dir in Path(VOICES_DIR).iterdir():
        if voice_dir.is_dir():
            voice_name = voice_dir.name
            status = get_voice_status(voice_name)
            
            voice_info = {
                "name": voice_name,
                "status": status,
                "samples": count_files(voice_dir / "samples"),
                "processed": count_files(voice_dir / "processed")
            }
            voices.append(voice_info)
    
    return {
        "voices": voices,
        "processing": is_processing(),
        "activeVoice": get_active_voice()
    }

@app.post("/api/voices/{voice_name}/sample")
async def save_voice_sample(voice_name: str, audio: UploadFile = File(...), text: str = None):
    """Save a voice sample recording or uploaded file"""
    # Create directories
    voice_dir = Path(VOICES_DIR) / voice_name
    samples_dir = voice_dir / "samples"
    os.makedirs(samples_dir, exist_ok=True)
    
    # Generate unique filename
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    filename = f"sample_{timestamp}.wav"
    filepath = samples_dir / filename
    
    # Process the audio file
    try:
        success = await process_audio_file(audio, filepath)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to process audio file")
        
        # Save metadata if text provided
        if text:
            metadata_file = samples_dir / f"{timestamp}_metadata.json"
            with open(metadata_file, "w") as f:
                json.dump({
                    "text": text, 
                    "timestamp": timestamp,
                    "original_filename": audio.filename
                }, f)
        
        return JSONResponse(content={
            "status": "success",
            "message": "Sample saved successfully",
            "filename": filename
        })
    except Exception as e:
        logger.error(f"Error saving voice sample: {str(e)}")
        # Clean up any created files
        if os.path.exists(filepath):
            os.unlink(filepath)
        raise HTTPException(status_code=500, detail=str(e))
        
@app.post("/api/voices/{voice_name}/process")
async def process_voice(voice_name: str, background_tasks: BackgroundTasks):
    """Start processing a voice"""
    voice_dir = Path(VOICES_DIR) / voice_name
    samples_dir = voice_dir / "samples"
    processed_dir = voice_dir / "processed"
    
    if not samples_dir.exists() or not list(samples_dir.glob("*.wav")):
        raise HTTPException(status_code=400, detail="No samples found for this voice")
    
    # Create processed directory
    os.makedirs(processed_dir, exist_ok=True)
    
    # Clear any previous status
    status_file = voice_dir / "status.json"
    if status_file.exists():
        os.remove(status_file)
    
    # Add processing task to background
    background_tasks.add_task(process_voice_task, voice_name)
    
    print(f"Starting processing task for voice: {voice_name}")
    print(f"Samples directory: {samples_dir}")
    print(f"Processed directory: {processed_dir}")
    sample_count = len(list(samples_dir.glob("*.wav")))
    print(f"Found {sample_count} WAV files to process")
    
    return JSONResponse(content={
        "status": "success",
        "message": f"Voice processing started for {voice_name}",
        "voice_name": voice_name,
        "samples_found": sample_count
    })

async def process_voice_task(voice_name: str):
    """Background task to process voice"""
    voice_dir = Path(VOICES_DIR) / voice_name
    samples_dir = voice_dir / "samples"
    processed_dir = voice_dir / "processed"
    
    try:
        # Step 1: First, check if we need to split large files using voice_sample_collector
        wav_files = list(samples_dir.glob("*.wav"))
        if wav_files:
            total_duration = 0
            for wav_file in wav_files:
                # Check file size as a rough estimate of duration
                file_size_mb = wav_file.stat().st_size / (1024 * 1024)
                if file_size_mb > 5:  # If file is larger than 5MB, assume it needs splitting
                    print(f"Processing large file {wav_file} with voice collector...")
                    collector_cmd = [
                        "docker", "exec", CONTAINER_NAME,
                        "python3", "/app/custom/voice_sample_collector.py",
                        "--input", f"/app/voice_samples/{voice_name}/samples/{wav_file.name}",
                        "--output_dir", f"/app/voice_samples/{voice_name}/samples/"
                    ]
                    
                    result = subprocess.run(collector_cmd, capture_output=True, text=True)
                    if result.returncode != 0:
                        print(f"Voice collector warning: {result.stderr}")
        
        # Step 2: Process audio files
        print(f"Preprocessing audio files for {voice_name}...")
        preprocess_cmd = [
            "docker", "exec", CONTAINER_NAME,
            "python3", "/app/custom/preprocess_audio.py",
            "--input_dir", f"/app/voice_samples/{voice_name}/samples/",
            "--output_dir", f"/app/voice_samples/{voice_name}/processed"
        ]
        
        result = subprocess.run(preprocess_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f"Preprocessing failed: {result.stderr}")
        
        print(f"Voice processing completed for {voice_name}")
        print(f"Output: {result.stdout}")
        
    except Exception as e:
        print(f"Error processing voice {voice_name}: {str(e)}")
        # Create a status file to indicate error
        status_file = voice_dir / "status.json"
        with open(status_file, 'w') as f:
            json.dump({
                "status": "error",
                "error": str(e),
                "timestamp": str(datetime.now())
            }, f)
        # Mark voice as error state if needed

@app.post("/api/voices/{voice_name}/activate")
async def activate_voice(voice_name: str):
    """Activate a voice for TTS"""
    voice_dir = Path(VOICES_DIR) / voice_name
    processed_dir = voice_dir / "processed"
    
    if not processed_dir.exists() or not list(processed_dir.glob("*.wav")):
        raise HTTPException(status_code=400, detail="Voice is not ready for activation")
    
    # Deactivate current voice first
    current_active = get_active_voice()
    if current_active:
        await deactivate_voice(current_active)
    
    # Activate new voice (restart TTS service with new voice)
    activate_cmd = [
        "docker", "exec", CONTAINER_NAME,
        "/app/custom/start-voice-service.sh",
        "--voice", f"/app/voice_samples/{voice_name}/processed",
        "--language", "en",
        "--gpu"
    ]
    
    # Run in background
    subprocess.Popen(activate_cmd)
    
    # Update active voice
    set_active_voice(voice_name)
    
    return JSONResponse(content={
        "status": "success",
        "message": f"Voice {voice_name} activated successfully"
    })

@app.post("/api/voices/{voice_name}/deactivate")
async def deactivate_voice(voice_name: str):
    """Deactivate a voice"""
    if get_active_voice() != voice_name:
        raise HTTPException(status_code=400, detail="Voice is not currently active")
    
    # Stop TTS service
    stop_cmd = ["docker", "exec", CONTAINER_NAME, "pkill", "-f", "voice_clone_server.py"]
    subprocess.run(stop_cmd)
    
    # Clear active voice
    set_active_voice(None)
    
    return JSONResponse(content={
        "status": "success",
        "message": f"Voice {voice_name} deactivated successfully"
    })

@app.delete("/api/voices/{voice_name}")
async def delete_voice(voice_name: str):
    """Delete a voice and all its data"""
    voice_dir = Path(VOICES_DIR) / voice_name
    
    if not voice_dir.exists():
        raise HTTPException(status_code=404, detail="Voice not found")
    
    # Ensure voice is not active
    if get_active_voice() == voice_name:
        raise HTTPException(status_code=400, detail="Cannot delete active voice")
    
    # Delete voice directory
    shutil.rmtree(voice_dir)
    
    return JSONResponse(content={
        "status": "success",
        "message": f"Voice {voice_name} deleted successfully"
    })

@app.post("/api/synthesize")
async def synthesize_speech(request: TTSRequest):
    """Generate speech from text"""
    # Validate text
    if not request.text:
        raise HTTPException(status_code=400, detail="No text provided")
    
    # Use requested voice or active voice
    voice = request.voice or get_active_voice()
    if not voice:
        raise HTTPException(status_code=400, detail="No voice specified and no active voice found")
    
    # Check if voice exists and is ready
    voice_status = get_voice_status(voice)
    if voice_status not in ["active", "ready"]:
        raise HTTPException(status_code=400, detail=f"Voice '{voice}' is not ready (status: {voice_status})")
    
    try:
        # Create temp file for audio
        fd, temp_path = tempfile.mkstemp(suffix='.wav', dir=Config.TEMP_DIR)
        os.close(fd)
        
        # Get voice samples directory
        voice_samples_dir = Path(Config.VOICES_DIR) / voice / "processed"
        if not voice_samples_dir.exists():
            raise HTTPException(status_code=500, detail=f"Voice samples directory not found for '{voice}'")
        
        # Get first sample for reference
        samples = list(voice_samples_dir.glob("*.wav"))
        if not samples:
            raise HTTPException(status_code=500, detail=f"No voice samples found for '{voice}'")
        
        sample_path = samples[0]
        
        logger.info(f"Generating speech for text: '{request.text[:30]}...' using voice: {voice}")
        
        # Try direct API call first (assuming voice service is already running)
        success = False
        try:
            # Try direct API call
            response = requests.post(
                Config.TTS_SERVICE_URL,
                json={"text": request.text, "language_variant": request.language_variant},
                timeout=300  # 5 second timeout
            )
            
            if response.status_code == 200:
                # Save response to temp file
                with open(temp_path, 'wb') as f:
                    f.write(response.content)
                logger.info(f"Speech generated successfully via API at {temp_path}")
                success = True
            else:
                # If API call fails, try with data instead of json format
                response = requests.post(
                    Config.TTS_SERVICE_URL,
                    data={"text": request.text, "language_variant": request.language_variant},
                    timeout=300
                )
                
                if response.status_code == 200:
                    # Save response to temp file
                    with open(temp_path, 'wb') as f:
                        f.write(response.content)
                    logger.info(f"Speech generated successfully via API with data format at {temp_path}")
                    success = True
                else:
                    raise Exception(f"TTS API call failed with both JSON and form data formats")
                
        except Exception as e:
            logger.warning(f"Direct API call failed: {e}. Starting voice clone server...")
            
            # Start voice clone server with the voice if it's not already running
            # We'll only do this if the voice is already active (to avoid reactivating)
            if voice_status == "active":
                try:
                    # Find all processed WAV files for the voice
                    voice_samples = list(voice_samples_dir.glob("*.wav"))
                    if not voice_samples:
                        raise HTTPException(status_code=500, detail=f"No processed voice samples found for '{voice}'")
                    
                    # Kill any existing voice server to ensure clean start
                    stop_cmd = ["docker", "exec", CONTAINER_NAME, "pkill", "-f", "voice_clone_server.py"]
                    subprocess.run(stop_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    
                    # Build sample paths as they appear in the container
                    sample_paths = [f"/app/voice_samples/{voice}/processed/{s.name}" for s in voice_samples]
                    sample_args = " ".join([f"--speaker_wav \"{p}\"" for p in sample_paths])
                    
                    language_variant = ""
                    if request.language_variant:
                        language_variant = f"--language_variant {request.language_variant}"
                    # Command to start voice server
                    start_cmd = [
                        "docker", "exec", CONTAINER_NAME,
                        "/bin/bash", "-c",
                        f"python3 /app/custom/voice_clone_server.py {sample_args} --language en {language_variant}"
                    ]
                    
                    # Start the server (non-blocking)
                    logger.info(f"Starting voice clone server")
                    subprocess.Popen(start_cmd)
                    
                    # Wait a bit for server to start
                    import time
                    time.sleep(5)
                    
                    # Try API call again
                    response = requests.post(
                        Config.TTS_SERVICE_URL,
                        data={"text": request.text, "language_variant": request.language_variant},
                        timeout=300
                    )
                    
                    if response.status_code != 200:
                        raise HTTPException(status_code=500, 
                                        detail=f"TTS API call failed after starting server: {response.status_code} - {response.text}")
                    
                    # Save response to temp file
                    with open(temp_path, 'wb') as f:
                        f.write(response.content)
                    logger.info(f"Speech generated successfully after starting server at {temp_path}")
                    success = True
                    
                except Exception as nested_e:
                    logger.error(f"Failed to start voice server and generate speech: {nested_e}")
                    raise HTTPException(status_code=500, detail=f"Failed to generate speech: {str(nested_e)}")
        
        if not success:
            raise HTTPException(status_code=500, detail="Failed to generate speech after multiple attempts")
        
        generated_dir = Config.get_generated_dir(voice)
        os.makedirs(generated_dir, exist_ok=True)
        
        # Create a unique filename
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # Create a safer filename from the text (first 30 chars)
        safe_text = "".join(c if c.isalnum() or c in [' ', '_', '-'] else '_' for c in request.text[:30])
        safe_text = safe_text.strip().replace(' ', '_')
        filename = f"{timestamp}_{safe_text}.wav"
        final_path = os.path.join(generated_dir, filename)
        
        # Copy from temp to final location
        shutil.copy2(temp_path, final_path)
        
        # Create metadata file with the full text
        metadata_path = os.path.join(generated_dir, f"{timestamp}_{safe_text}.json")
        with open(metadata_path, 'w') as f:
            json.dump({
                "text": request.text,
                "voice": voice,
                "date": datetime.now().isoformat(),
                "filename": filename
            }, f)
        
        # Set file permissions
        os.chmod(final_path, 0o644)
        os.chmod(metadata_path, 0o644)
        
        logger.info(f"Saved generated audio to {final_path}")
        
        # Return the audio file
        return FileResponse(
            temp_path,
            media_type="audio/wav",
            filename=f"tts_{voice}_{len(request.text)}.wav"
        )
    
    except Exception as e:
        logger.error(f"Error in synthesize_speech: {e}")
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=f"Failed to generate speech: {str(e)}")

@app.get("/api/voices/{voice_name}/history")
async def get_voice_history(voice_name: str) -> List[HistoryItem]:
    """Get history of generated audio for a voice"""
    try:
        generated_dir = Config.get_generated_dir(voice_name)
        if not os.path.exists(generated_dir):
            return []
        
        history_items = []
        
        # Get all JSON metadata files
        json_files = sorted(Path(generated_dir).glob("*.json"), key=os.path.getmtime, reverse=True)
        
        for json_file in json_files:
            try:
                with open(json_file, 'r') as f:
                    metadata = json.load(f)
                
                # Check if the audio file exists
                audio_path = os.path.join(generated_dir, metadata.get("filename", ""))
                if os.path.exists(audio_path):
                    # Create history item
                    item = HistoryItem(
                        id=os.path.splitext(metadata.get("filename", ""))[0],
                        text=metadata.get("text", ""),
                        filename=metadata.get("filename", ""),
                        date=metadata.get("date", "")
                    )
                    history_items.append(item)
            except Exception as e:
                logger.error(f"Error reading metadata file {json_file}: {e}")
                continue
        
        return history_items
    
    except Exception as e:
        logger.error(f"Error getting voice history: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get voice history: {str(e)}")

@app.get("/api/voices/{voice_name}/audio/{filename}")
async def get_voice_audio(voice_name: str, filename: str):
    """Get a specific audio file"""
    try:
        generated_dir = Config.get_generated_dir(voice_name)
        audio_path = os.path.join(generated_dir, filename)
        
        if not os.path.exists(audio_path):
            raise HTTPException(status_code=404, detail="Audio file not found")
        
        return FileResponse(
            audio_path,
            media_type="audio/wav",
            filename=filename
        )
    
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        logger.error(f"Error getting voice audio: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get audio file: {str(e)}")

@app.delete("/api/voices/{voice_name}/audio/{filename}")
async def delete_voice_audio(voice_name: str, filename: str):
    """Delete a specific audio file"""
    try:
        generated_dir = Config.get_generated_dir(voice_name)
        audio_path = os.path.join(generated_dir, filename)
        metadata_path = os.path.join(generated_dir, os.path.splitext(filename)[0] + ".json")
        
        # Check if file exists
        if not os.path.exists(audio_path):
            raise HTTPException(status_code=404, detail="Audio file not found")
        
        # Delete files
        os.remove(audio_path)
        if os.path.exists(metadata_path):
            os.remove(metadata_path)
        
        return {"status": "success", "message": "Audio file deleted successfully"}
    
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        logger.error(f"Error deleting voice audio: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to delete audio file: {str(e)}")
        
@app.get("/api/tts-health")
async def check_tts_health(voice: str = None):
    """Check if TTS service is ready for a specific voice"""
    try:
        # Simple health check - try to connect to the TTS service with a 2-second timeout
        response = requests.get(
            Config.TTS_SERVICE_URL.replace('/api/tts', '/'),  # Get the base URL
            timeout=2  # Short timeout
        )
        
        # If we get any response, the service is up
        if response.status_code < 500:  # Any response code below 500 means server is up
            return {"status": "ready", "voice": voice}
        else:
            return {"status": "not_ready", "voice": voice}
    except requests.exceptions.RequestException:
        # Connection failed
        return {"status": "not_ready", "voice": voice}

async def process_audio_file(audio: UploadFile, output_filepath: Path) -> bool:
    """
    Process an uploaded audio file, converting it to the proper format if needed.
    Returns True if successful, False otherwise.
    """
    try:
        # Create temp file for the uploaded audio
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(audio.filename)[1])
        temp_filename = temp_file.name
        temp_file.close()
        
        # Save uploaded file to temp file
        content = await audio.read()
        with open(temp_filename, "wb") as f:
            f.write(content)
        
        # Check if conversion is needed
        _, ext = os.path.splitext(audio.filename)
        if ext.lower() != '.wav':
            logger.info(f"Converting {audio.filename} from {ext} to WAV format")
            
            # Use ffmpeg to convert to proper WAV format
            convert_cmd = [
                "ffmpeg", "-i", temp_filename,
                "-ar", "22050",  # Sample rate
                "-ac", "1",      # Mono
                "-c:a", "pcm_s16le",  # 16-bit PCM
                str(output_filepath)
            ]
            
            result = subprocess.run(convert_cmd, capture_output=True, text=True)
            if result.returncode != 0:
                logger.error(f"Failed to convert audio: {result.stderr}")
                raise Exception(f"Audio conversion failed: {result.stderr}")
                
            # Clean up temp file
            os.unlink(temp_filename)
        else:
            # Just move the file if it's already WAV
            os.rename(temp_filename, output_filepath)
        
        return True
        
    except Exception as e:
        logger.error(f"Error processing audio file: {str(e)}")
        # Clean up temp file if it exists
        if 'temp_filename' in locals() and os.path.exists(temp_filename):
            os.unlink(temp_filename)
        return False     
        
@app.get("/api/voices/{voice_name}/samples", response_model=SamplesResponse)
async def get_voice_samples(voice_name: str):
    """Get all samples for a voice"""
    try:
        voice_dir = Path(Config.VOICES_DIR) / voice_name
        samples_dir = voice_dir / "samples"
        
        if not samples_dir.exists():
            return {"samples": [], "count": 0}
        
        samples = []
        
        # Get all WAV files in the samples directory
        wav_files = list(samples_dir.glob("*.wav"))
        
        for wav_file in wav_files:
            sample_info = {
                "filename": wav_file.name,
                "created_at": datetime.fromtimestamp(os.path.getctime(wav_file)).isoformat()
            }
            
            # Check for metadata file
            metadata_filename = os.path.splitext(wav_file.name)[0] + "_metadata.json"
            metadata_path = samples_dir / metadata_filename
            if metadata_path.exists():
                try:
                    with open(metadata_path, 'r') as f:
                        metadata = json.load(f)
                        sample_info["metadata"] = metadata
                except:
                    # If metadata file is corrupted, continue without it
                    pass
            
            samples.append(sample_info)
        
        # Sort by creation time (newest first)
        samples.sort(key=lambda x: x["created_at"], reverse=True)
        
        return {
            "samples": samples,
            "count": len(samples)
        }
    
    except Exception as e:
        logger.error(f"Error getting voice samples: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to get voice samples: {str(e)}")

@app.get("/api/voices/{voice_name}/sample/{filename}")
async def get_voice_sample(voice_name: str, filename: str):
    """Get a specific sample audio file"""
    try:
        voice_dir = Path(Config.VOICES_DIR) / voice_name
        samples_dir = voice_dir / "samples"
        file_path = samples_dir / filename
        
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="Sample not found")
        
        return FileResponse(
            file_path,
            media_type="audio/wav",
            filename=filename
        )
    
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        logger.error(f"Error getting voice sample: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to get voice sample: {str(e)}")

@app.delete("/api/voices/{voice_name}/sample/{filename}")
async def delete_voice_sample(voice_name: str, filename: str):
    """Delete a specific sample"""
    try:
        voice_dir = Path(Config.VOICES_DIR) / voice_name
        samples_dir = voice_dir / "samples"
        file_path = samples_dir / filename
        
        # Check if the file exists
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="Sample not found")
        
        # Check for metadata file
        metadata_filename = os.path.splitext(filename)[0] + "_metadata.json"
        metadata_path = samples_dir / metadata_filename
        
        # Delete files
        os.remove(file_path)
        if metadata_path.exists():
            os.remove(metadata_path)
        
        return {
            "status": "success",
            "message": f"Sample {filename} deleted successfully"
        }
    
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        logger.error(f"Error deleting voice sample: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to delete voice sample: {str(e)}")

@app.delete("/api/voices/{voice_name}/samples")
async def delete_all_voice_samples(voice_name: str):
    """Delete all samples for a voice"""
    try:
        voice_dir = Path(Config.VOICES_DIR) / voice_name
        samples_dir = voice_dir / "samples"
        
        if not samples_dir.exists():
            return {
                "status": "success",
                "message": "No samples to delete"
            }
        
        # Get all files in the samples directory
        files = list(samples_dir.glob("*"))
        deleted_count = 0
        
        for file in files:
            try:
                os.remove(file)
                deleted_count += 1
            except:
                # If we can't delete a file, continue with others
                pass
        
        return {
            "status": "success",
            "message": f"Deleted {deleted_count} files successfully"
        }
    
    except Exception as e:
        logger.error(f"Error deleting all voice samples: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to delete all voice samples: {str(e)}")
 
@app.get("/api/voices/{voice_name}/processed")
async def get_processed_files(voice_name: str):
    """Get all processed files for a voice"""
    try:
        voice_dir = Path(Config.VOICES_DIR) / voice_name
        processed_dir = voice_dir / "processed"
        
        if not processed_dir.exists():
            return {"files": [], "count": 0}
        
        files = []
        
        # Get all WAV files in the processed directory
        wav_files = list(processed_dir.glob("*.wav"))
        
        for wav_file in wav_files:
            file_info = {
                "filename": wav_file.name,
                "created_at": datetime.fromtimestamp(os.path.getctime(wav_file)).isoformat()
            }
            files.append(file_info)
        
        # Sort by creation time (newest first)
        files.sort(key=lambda x: x["created_at"], reverse=True)
        
        return {
            "files": files,
            "count": len(files)
        }
    
    except Exception as e:
        logger.error(f"Error getting processed files: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to get processed files: {str(e)}")

@app.get("/api/voices/{voice_name}/processed/{filename}")
async def get_processed_file(voice_name: str, filename: str):
    """Get a specific processed audio file"""
    try:
        voice_dir = Path(Config.VOICES_DIR) / voice_name
        processed_dir = voice_dir / "processed"
        file_path = processed_dir / filename
        
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="Processed file not found")
        
        return FileResponse(
            file_path,
            media_type="audio/wav",
            filename=filename
        )
    
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        logger.error(f"Error getting processed file: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to get processed file: {str(e)}")
        
# Run the server
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)