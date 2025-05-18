#!/usr/bin/env python3
"""
Voice Dispatcher Service for Multiple TTS Voices
- Routes requests to the appropriate voice service
- Provides a unified API for all voices
- Supports voice selection in requests
"""
import os
import sys
import json
import logging
import hashlib
import tempfile
import time
import requests
from flask import Flask, request, send_file, jsonify

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('/var/log/voice-dispatcher.log')
    ]
)
logger = logging.getLogger('voice_dispatcher')

app = Flask(__name__)

# Configuration
VOICE_SERVICES_FILE = os.environ.get("VOICE_SERVICES_FILE", "/app/logs/voice_services.json")
DEFAULT_VOICE = os.environ.get("DEFAULT_VOICE", "")  # Empty means first available
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "180"))  # 3 minutes
CACHE_DIR = os.environ.get("CACHE_DIR", "/tmp/voice-cache")
PORT = int(os.environ.get("PORT", "5002"))  # Same port as expected by the bridge

# Create cache directory if it doesn't exist
os.makedirs(CACHE_DIR, exist_ok=True)

# Global variable to store voice services
voice_services = []

def load_voice_services():
    """Load voice services from JSON file"""
    global voice_services
    
    try:
        if os.path.exists(VOICE_SERVICES_FILE):
            with open(VOICE_SERVICES_FILE, 'r') as f:
                voice_services = json.load(f)
                
            # Add URLs to each service
            for service in voice_services:
                service['url'] = f"http://localhost:{service['port']}/api/tts"
                
            logger.info(f"Loaded {len(voice_services)} voice services")
            return True
        else:
            logger.error(f"Voice services file not found: {VOICE_SERVICES_FILE}")
            return False
    except Exception as e:
        logger.error(f"Error loading voice services: {str(e)}")
        return False

def get_voice_service(voice_name=None):
    """Get a voice service by name, or default if not specified"""
    global voice_services
    
    # If no services available, try to load them
    if not voice_services:
        if not load_voice_services():
            return None
    
    # If still no services, return None
    if not voice_services:
        return None
    
    # If no voice specified or not found, use default or first available
    if not voice_name:
        # Use default voice if specified
        if DEFAULT_VOICE and any(s['name'] == DEFAULT_VOICE for s in voice_services):
            return next(s for s in voice_services if s['name'] == DEFAULT_VOICE)
        # Otherwise use first available
        return voice_services[0]
    
    # Find the requested voice
    for service in voice_services:
        if service['name'].lower() == voice_name.lower():
            return service
    
    # Voice not found, use default
    logger.warning(f"Voice '{voice_name}' not found, using default")
    return get_voice_service(None)

def get_cache_path(text, voice_name):
    """Generate a cache path based on text and voice"""
    # Create a unique hash from the text and voice
    voice_hash = hashlib.md5(voice_name.encode()).hexdigest()
    text_hash = hashlib.md5(text.encode()).hexdigest()
    return os.path.join(CACHE_DIR, f"{text_hash}_{voice_hash}.wav")

def check_service_health(service):
    """Check if a voice service is healthy"""
    try:
        health_url = service['url'].replace('/api/tts', '/health')
        response = requests.get(health_url, timeout=5)
        return response.status_code == 200
    except:
        return False

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint with information about all voice services"""
    # Refresh voice services
    load_voice_services()
    
    # Check health of each service
    services_status = []
    for service in voice_services:
        healthy = check_service_health(service)
        services_status.append({
            "name": service['name'],
            "port": service['port'],
            "healthy": healthy,
            "samples": service.get('samples', 0)
        })
    
    # Count cache files
    cache_files = len([f for f in os.listdir(CACHE_DIR) if f.endswith('.wav')])
    
    return jsonify({
        "status": "healthy" if any(s['healthy'] for s in services_status) else "unhealthy",
        "voice_services": services_status,
        "default_voice": DEFAULT_VOICE or "first available",
        "timeout": REQUEST_TIMEOUT,
        "cache_dir": CACHE_DIR,
        "cache_files": cache_files,
        "available_voices": [s['name'] for s in services_status if s['healthy']]
    })

@app.route('/api/voices', methods=['GET'])
def list_voices():
    """List all available voices"""
    # Refresh voice services
    load_voice_services()
    
    voices = []
    for service in voice_services:
        if check_service_health(service):
            voices.append({
                "name": service['name'],
                "samples": service.get('samples', 0)
            })
    
    return jsonify({
        "voices": voices,
        "default": DEFAULT_VOICE or voices[0]['name'] if voices else None
    })

@app.route('/api/tts', methods=['POST'])
def tts_endpoint():
    """Unified TTS endpoint that routes to the appropriate voice service"""
    start_time = time.time()
    
    # Get text from request
    text = None
    voice_name = None
    
    if request.is_json:
        data = request.json
        text = data.get('text')
        voice_name = data.get('voice')
    else:
        text = request.form.get('text')
        voice_name = request.form.get('voice')
    
    if not text:
        logger.warning("Request received with no text")
        return jsonify({"error": "No text provided"}), 400
    
    # Generate a unique ID for this request
    request_id = hashlib.md5((text + str(time.time())).encode()).hexdigest()[:8]
    
    # Get the voice service
    service = get_voice_service(voice_name)
    if not service:
        logger.error(f"[{request_id}] No voice services available")
        return jsonify({"error": "No voice services available"}), 503
    
    # Get actual voice name from service
    actual_voice = service['name']
    
    # Log request
    logger.info(f"[{request_id}] TTS request: voice='{actual_voice}', text='{text[:50]}...' ({len(text)} chars)")
    
    # Check cache
    cache_file = get_cache_path(text, actual_voice)
    if os.path.exists(cache_file):
        logger.info(f"[{request_id}] Cache hit for voice '{actual_voice}': '{text[:30]}...'")
        return send_file(cache_file, mimetype='audio/wav')
    
    try:
        # Create a temporary file for the audio
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as temp:
            temp_wav = temp.name
        
        # Forward request to the appropriate voice service
        service_url = service['url']
        logger.info(f"[{request_id}] Forwarding to voice service '{actual_voice}' at {service_url}")
        
        # Prepare the request
        tts_request_data = json.dumps({"text": text})
        headers = {'Content-Type': 'application/json'}
        
        try:
            response = requests.post(
                service_url,
                data=tts_request_data,
                headers=headers,
                stream=True,
                timeout=REQUEST_TIMEOUT
            )
            
            if response.status_code != 200:
                logger.error(f"[{request_id}] Voice service error: {response.status_code} - {response.text}")
                return jsonify({"error": f"Voice service error: {response.text}"}), 500
            
            # Save the audio file
            with open(temp_wav, 'wb') as f:
                for chunk in response.iter_content(8192):
                    f.write(chunk)
            
            # Copy to cache
            with open(temp_wav, 'rb') as src, open(cache_file, 'wb') as dst:
                dst.write(src.read())
            
            # Return the audio file
            logger.info(f"[{request_id}] Synthesis successful in {time.time() - start_time:.3f}s")
            return send_file(temp_wav, mimetype='audio/wav')
            
        except requests.exceptions.RequestException as e:
            logger.error(f"[{request_id}] Voice service request failed: {str(e)}")
            return jsonify({"error": f"Voice service request failed: {str(e)}"}), 502
            
    except Exception as e:
        logger.error(f"[{request_id}] Error: {str(e)}", exc_info=True)
        return jsonify({"error": str(e)}), 500
    finally:
        # Clean up temporary file
        if 'temp_wav' in locals() and os.path.exists(temp_wav):
            os.unlink(temp_wav)

def main():
    global DEFAULT_VOICE, REQUEST_TIMEOUT, VOICE_SERVICES_FILE, CACHE_DIR, PORT
    
    # Parse command line arguments
    import argparse
    parser = argparse.ArgumentParser(description="Voice Dispatcher Service")
    parser.add_argument("--services-file", help="Path to voice services JSON file")
    parser.add_argument("--default-voice", help="Default voice to use")
    parser.add_argument("--timeout", type=int, help="Request timeout in seconds")
    parser.add_argument("--cache-dir", help="Cache directory")
    parser.add_argument("--port", type=int, help="Port to listen on")
    args = parser.parse_args()
    
    # Update configuration from arguments
    if args.services_file:
        VOICE_SERVICES_FILE = args.services_file
    if args.default_voice:
        DEFAULT_VOICE = args.default_voice
    if args.timeout:
        REQUEST_TIMEOUT = args.timeout
    if args.cache_dir:
        CACHE_DIR = args.cache_dir
        os.makedirs(CACHE_DIR, exist_ok=True)
    if args.port:
        PORT = args.port
    
    # Load voice services
    if not load_voice_services():
        logger.warning("No voice services loaded, will retry when requests come in")
    
    # Log configuration
    logger.info(f"Starting Voice Dispatcher Service on port {PORT}")
    logger.info(f"Voice services file: {VOICE_SERVICES_FILE}")
    logger.info(f"Default voice: {DEFAULT_VOICE or 'first available'}")
    logger.info(f"Request timeout: {REQUEST_TIMEOUT} seconds")
    logger.info(f"Cache directory: {CACHE_DIR}")
    
    # List available voices
    if voice_services:
        logger.info(f"Available voices: {', '.join(s['name'] for s in voice_services)}")
    
    # Start server
    app.run(host="0.0.0.0", port=PORT, threaded=True)

if __name__ == "__main__":
    main()