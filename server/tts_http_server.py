#!/usr/bin/env python3
"""
Improved HTTP server with timeout handling for Asterisk TTS integration
Features:
- Increased timeout for TTS requests
- Text chunking for long inputs
- Better error recovery
- Resource monitoring
- Fallback options
"""
import os
import sys
import logging
import subprocess
import tempfile
import time
import hashlib
import json
import re
import psutil
import requests
from flask import Flask, request, send_file, jsonify
from threading import Thread
from queue import Queue, Empty

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('/var/log/tts-bridge.log')
    ]
)
logger = logging.getLogger('tts_bridge')

app = Flask(__name__)

# Configuration
TTS_DOCKER_URL = os.environ.get("TTS_DOCKER_URL", "http://localhost:5002/api/tts")
AUDIO_DIR = os.environ.get("TTS_AUDIO_DIR", "/tmp/asterisk-tts")
CACHE_DIR = os.environ.get("TTS_CACHE_DIR", "/tmp/asterisk-tts-cache")
SAMPLE_RATE = os.environ.get("TTS_SAMPLE_RATE", "8000")
AUDIO_FORMAT = os.environ.get("TTS_AUDIO_FORMAT", "wav")
TTS_REQUEST_TIMEOUT = int(os.environ.get("TTS_REQUEST_TIMEOUT", "300"))  # Increased timeout
MAX_TEXT_LENGTH = int(os.environ.get("MAX_TEXT_LENGTH", "200"))  # Max chars per chunk
ENABLE_CHUNKING = os.environ.get("ENABLE_CHUNKING", "true").lower() == "true"

# Create necessary directories
os.makedirs(AUDIO_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

def get_resource_usage():
    """Get current CPU and memory usage"""
    cpu_percent = psutil.cpu_percent(interval=0.1)
    memory_percent = psutil.virtual_memory().percent
    return {
        "cpu_percent": cpu_percent,
        "memory_percent": memory_percent,
        "high_load": cpu_percent > 80 or memory_percent > 80
    }

def get_cache_path(text):
    """Generate a cache path based on the text"""
    text_hash = hashlib.md5(text.encode()).hexdigest()
    return os.path.join(CACHE_DIR, f"{text_hash}.{AUDIO_FORMAT}")

def convert_audio(input_file, output_file, sample_rate=8000):
    """Convert audio to format compatible with Asterisk"""
    try:
        # Format conversion command
        cmd = [
            "sox", 
            input_file, 
            "-r", str(sample_rate),
            "-c", "1",
            output_file
        ]
        
        # Run conversion
        logger.debug(f"Converting audio with command: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            logger.error(f"Audio conversion failed: {result.stderr}")
            return False
            
        return True
    except Exception as e:
        logger.error(f"Error converting audio: {str(e)}")
        return False

def concatenate_audio_files(input_files, output_file):
    """Concatenate multiple WAV files into one"""
    try:
        # Create a list of input files for sox
        cmd = ["sox"]
        for f in input_files:
            cmd.append(f)
        cmd.append(output_file)
        
        # Run the command
        logger.debug(f"Concatenating audio with command: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            logger.error(f"Audio concatenation failed: {result.stderr}")
            return False
            
        return True
    except Exception as e:
        logger.error(f"Error concatenating audio: {str(e)}")
        return False

def chunk_text(text, max_length=MAX_TEXT_LENGTH):
    """Split text into smaller chunks at sentence boundaries"""
    if len(text) <= max_length:
        return [text]
    
    # Split by sentence
    sentences = re.split(r'(?<=[.!?])\s+', text)
    chunks = []
    current_chunk = ""
    
    for sentence in sentences:
        # If the sentence alone is too long, split by comma
        if len(sentence) > max_length:
            comma_parts = re.split(r'(?<=,)\s+', sentence)
            for part in comma_parts:
                # If still too long, just split by length
                if len(part) > max_length:
                    while part:
                        chunks.append(part[:max_length])
                        part = part[max_length:]
                else:
                    if len(current_chunk) + len(part) > max_length:
                        chunks.append(current_chunk)
                        current_chunk = part
                    else:
                        current_chunk += " " + part if current_chunk else part
        else:
            if len(current_chunk) + len(sentence) > max_length:
                chunks.append(current_chunk)
                current_chunk = sentence
            else:
                current_chunk += " " + sentence if current_chunk else sentence
    
    # Add the last chunk if not empty
    if current_chunk:
        chunks.append(current_chunk)
    
    return chunks

def synthesize_text_chunk(text, voice, temp_dir, result_queue, timeout=TTS_REQUEST_TIMEOUT):
    """Synthesize a single chunk of text and put result in queue"""
    try:
        # Create temp file path
        temp_wav = os.path.join(temp_dir, f"chunk_{hashlib.md5(text.encode()).hexdigest()[:8]}.wav")
        
        # Prepare the request
        tts_request_data = json.dumps({"text": text, "voice": voice})
        headers = {'Content-Type': 'application/json'}
        
        # Log the request
        logger.info(f"Sending chunk to TTS service: '{text[:30]}...' ({len(text)} chars)")
        
        # Make the request with increased timeout
        response = requests.post(
            TTS_DOCKER_URL,
            data=tts_request_data,
            headers=headers,
            stream=True,
            timeout=timeout
        )
        
        if response.status_code != 200:
            logger.error(f"TTS service error: {response.status_code} - {response.text}")
            result_queue.put({"success": False, "error": response.text})
            return
        
        # Save the audio file
        with open(temp_wav, 'wb') as f:
            for chunk in response.iter_content(8192):
                f.write(chunk)
        
        # Signal completion
        result_queue.put({"success": True, "file": temp_wav})
        logger.info(f"Chunk synthesis complete: '{text[:30]}...'")
        
    except Exception as e:
        logger.error(f"Error in chunk synthesis: {str(e)}", exc_info=True)
        result_queue.put({"success": False, "error": str(e)})

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint with resource monitoring"""
    # Check resource usage
    resources = get_resource_usage()
    
    # Check TTS service
    tts_status = "unknown"
    try:
        response = requests.get(
            TTS_DOCKER_URL.replace('/api/tts', '/health'), 
            timeout=300
        )
        if response.status_code == 200:
            tts_status = "healthy"
        else:
            tts_status = f"unhealthy (status: {response.status_code})"
    except requests.exceptions.RequestException:
        tts_status = "unreachable"
    
    # Check disk space
    disk_space = subprocess.run(
        ["df", "-h", AUDIO_DIR], 
        capture_output=True, 
        text=True
    ).stdout
    
    return jsonify({
        "status": "healthy" if not resources["high_load"] else "overloaded",
        "tts_service": tts_status,
        "audio_dir": AUDIO_DIR,
        "cache_dir": CACHE_DIR,
        "resources": resources,
        "disk_space": disk_space.split("\n")[1] if len(disk_space.split("\n")) > 1 else "unknown"
    })

@app.route('/tts', methods=['GET', 'POST'])
def tts_endpoint():
    """
    Endpoint for text-to-speech synthesis with improved timeout handling
    """
    start_time = time.time()
    
    # Get text from request (either GET param or POST data)
    text = None
    voice = None
    
    if request.method == 'GET':
        text = request.args.get('text', '')
        voice = request.args.get('voice', '')
    else:
        text = request.form.get('text', '')
        voice = request.form.get('voice', '')
        
        # If no form data, try JSON
        if not text and request.is_json:
            text = request.json.get('text', '')
            voice = request.json.get('voice', '')
            
    if not text:
        logger.warning("Request received with no text")
        return jsonify({"error": "No text provided"}), 400
        
    if not voice:
        logger.warning("Request received with no voice name")
        return jsonify({"error": "No voice name provided"}), 400
    
    # Check resources before proceeding
    resources = get_resource_usage()
    if resources["high_load"]:
        logger.warning(f"System under high load: CPU {resources['cpu_percent']}%, Memory {resources['memory_percent']}%")
    
    # Log request
    logger.info(f"TTS request: '{text[:50]}...' ({len(text)} chars)")
    
    # Check cache
    cache_file = get_cache_path(text)
    if os.path.exists(cache_file):
        logger.info(f"Cache hit for text: '{text[:30]}...' - using {cache_file}")
        return jsonify({
            "status": "success",
            "file": cache_file,
            "cached": True,
            "time": f"{time.time() - start_time:.3f}s"
        })
    
    try:
        # Create a temporary directory for this request
        with tempfile.TemporaryDirectory(dir=AUDIO_DIR) as temp_dir:
            # Create output filename
            output_file = os.path.join(temp_dir, "output.wav")
            
            # Determine if we need to chunk the text
            if ENABLE_CHUNKING and len(text) > MAX_TEXT_LENGTH:
                # Split text into chunks
                chunks = chunk_text(text)
                logger.info(f"Split text into {len(chunks)} chunks")
                
                chunk_files = []
                result_queue = Queue()
                threads = []
                
                # Start a thread for each chunk
                for i, chunk_text in enumerate(chunks):
                    thread = Thread(
                        target=synthesize_text_chunk,
                        args=(chunk_text, voice, temp_dir, result_queue)
                    )
                    thread.daemon = True
                    thread.start()
                    threads.append(thread)
                
                # Wait for all threads to complete
                for i, thread in enumerate(threads):
                    thread.join(TTS_REQUEST_TIMEOUT + 10)  # Give extra time for thread to complete
                    if thread.is_alive():
                        logger.error(f"Thread for chunk {i} timed out")
                
                # Collect results
                success = True
                for _ in range(len(chunks)):
                    try:
                        result = result_queue.get(block=False)
                        if result["success"]:
                            chunk_files.append(result["file"])
                        else:
                            success = False
                            logger.error(f"Chunk synthesis failed: {result.get('error', 'Unknown error')}")
                    except Empty:
                        success = False
                        logger.error("Missing result from synthesis thread")
                
                if not success or len(chunk_files) != len(chunks):
                    return jsonify({"error": "Failed to synthesize one or more chunks"}), 500
                
                # Concatenate all chunk files
                if not concatenate_audio_files(chunk_files, output_file):
                    return jsonify({"error": "Failed to concatenate audio chunks"}), 500
                
            else:
                # Process the entire text at once
                result_queue = Queue()
                
                # Start synthesis in a separate thread
                thread = Thread(
                    target=synthesize_text_chunk,
                    args=(text, voice, temp_dir, result_queue, TTS_REQUEST_TIMEOUT)
                )
                thread.daemon = True
                thread.start()
                thread.join(TTS_REQUEST_TIMEOUT + 10)  # Wait with some extra buffer time
                
                # Check if thread completed successfully
                if thread.is_alive():
                    logger.error(f"Synthesis thread timed out after {TTS_REQUEST_TIMEOUT} seconds")
                    return jsonify({"error": f"TTS request timed out after {TTS_REQUEST_TIMEOUT} seconds"}), 500
                
                try:
                    result = result_queue.get(block=False)
                    if not result["success"]:
                        return jsonify({"error": result.get("error", "Unknown error")}), 500
                    output_file = result["file"]
                except Empty:
                    logger.error("No result from synthesis thread")
                    return jsonify({"error": "No result from synthesis thread"}), 500
            
            # Convert to Asterisk format
            converted_filename = cache_file
            if not convert_audio(output_file, converted_filename, SAMPLE_RATE):
                return jsonify({"error": "Failed to convert audio"}), 500
            
            # Log successful synthesis
            logger.info(f"Synthesis successful in {time.time() - start_time:.3f}s")
            
            # Return the path for Asterisk to use
            return jsonify({
                "status": "success",
                "file": converted_filename,
                "cached": False,
                "time": f"{time.time() - start_time:.3f}s"
            })
    except Exception as e:
        logger.error(f"Error in TTS process: {str(e)}", exc_info=True)
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    logger.info(f"Starting TTS HTTP bridge server on port 5003")
    logger.info(f"TTS service URL: {TTS_DOCKER_URL}")
    logger.info(f"Request timeout: {TTS_REQUEST_TIMEOUT} seconds")
    logger.info(f"Text chunking: {'Enabled' if ENABLE_CHUNKING else 'Disabled'}")
    if ENABLE_CHUNKING:
        logger.info(f"Max chunk length: {MAX_TEXT_LENGTH} characters")
    app.run(host="0.0.0.0", port=5003)