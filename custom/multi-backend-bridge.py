#!/usr/bin/env python3
"""
Multi-Backend TTS HTTP Bridge Server
Features:
- Can use multiple TTS backends with fallback
- Automatic failure detection and recovery
- Circuit breaker pattern for failing backends
- Performance-based routing
"""
import os
import sys
import logging
import subprocess
import tempfile
import time
import hashlib
import json
import requests
import psutil
from datetime import datetime, timedelta
from flask import Flask, request, send_file, jsonify

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
BACKENDS = [
    {
        "name": "primary",
        "url": "http://localhost:5002/api/tts",
        "timeout": 30,
        "enabled": True,
        "weight": 100,  # Higher weight = higher priority
        "failures": 0,
        "last_failure": None,
        "success_rate": 100.0,
        "avg_response_time": 0.0,
        "requests": 0
    },
    {
        "name": "lightweight",
        "url": "http://localhost:5003/api/tts",
        "timeout": 20,
        "enabled": True,
        "weight": 50,
        "failures": 0,
        "last_failure": None,
        "success_rate": 100.0,
        "avg_response_time": 0.0,
        "requests": 0
    },
    {
        "name": "fallback",
        "url": "http://localhost:5004/api/tts",
        "timeout": 10,
        "enabled": True,
        "weight": 10,
        "failures": 0, 
        "last_failure": None,
        "success_rate": 100.0,
        "avg_response_time": 0.0,
        "requests": 0
    }
]

AUDIO_DIR = os.environ.get("TTS_AUDIO_DIR", "/tmp/asterisk-tts")
CACHE_DIR = os.environ.get("TTS_CACHE_DIR", "/tmp/asterisk-tts-cache")
SAMPLE_RATE = int(os.environ.get("TTS_SAMPLE_RATE", "8000"))
AUDIO_FORMAT = os.environ.get("TTS_AUDIO_FORMAT", "wav")
MAX_FAILURES = int(os.environ.get("TTS_MAX_FAILURES", "3"))
CIRCUIT_BREAKER_TIMEOUT = int(os.environ.get("TTS_CIRCUIT_BREAKER_TIMEOUT", "300"))  # seconds

# Create necessary directories
os.makedirs(AUDIO_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

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

def get_resource_usage():
    """Get current resource usage"""
    cpu_percent = psutil.cpu_percent(interval=0.1)
    memory_percent = psutil.virtual_memory().percent
    return {
        "cpu_percent": cpu_percent,
        "memory_percent": memory_percent,
        "high_load": cpu_percent > 80 or memory_percent > 80
    }

def select_backend(text_length):
    """Select the best backend based on performance metrics and text length"""
    # Filter out disabled backends
    available_backends = [b for b in BACKENDS if b["enabled"]]
    
    if not available_backends:
        logger.error("No TTS backends available")
        return None
    
    # For very short text, prefer faster backends
    if text_length < 50:
        # Sort by response time (faster first)
        sorted_backends = sorted(available_backends, 
                                 key=lambda b: b["avg_response_time"] if b["requests"] > 0 else 999)
    else:
        # Complex scoring formula considering success rate, response time, and weight
        for backend in available_backends:
            backend["score"] = (
                (backend["success_rate"] * 0.6) + 
                ((1 - min(backend["avg_response_time"], 10) / 10) * 0.2) +
                (backend["weight"] / 100 * 0.2)
            ) if backend["requests"] > 0 else (backend["weight"] / 100)
        
        sorted_backends = sorted(available_backends, key=lambda b: b["score"], reverse=True)
    
    return sorted_backends[0] if sorted_backends else None

def check_circuit_breaker(backend):
    """Check if a backend should be re-enabled after circuit breaker timeout"""
    if backend["enabled"]:
        return True
        
    # If disabled due to failures, check if timeout has expired
    if backend["last_failure"]:
        now = datetime.now()
        timeout_delta = timedelta(seconds=CIRCUIT_BREAKER_TIMEOUT)
        
        if now - backend["last_failure"] > timeout_delta:
            logger.info(f"Re-enabling backend '{backend['name']}' after circuit breaker timeout")
            backend["enabled"] = True
            return True
    
    return False

def update_backend_stats(backend_name, success, response_time):
    """Update backend statistics"""
    for backend in BACKENDS:
        if backend["name"] == backend_name:
            backend["requests"] += 1
            
            # Update success rate
            if backend["requests"] == 1:
                backend["success_rate"] = 100.0 if success else 0.0
            else:
                backend["success_rate"] = (
                    backend["success_rate"] * (backend["requests"] - 1) + (100.0 if success else 0.0)
                ) / backend["requests"]
            
            # Update average response time
            if backend["requests"] == 1:
                backend["avg_response_time"] = response_time
            else:
                backend["avg_response_time"] = (
                    backend["avg_response_time"] * (backend["requests"] - 1) + response_time
                ) / backend["requests"]
            
            # Update failure count
            if not success:
                backend["failures"] += 1
                backend["last_failure"] = datetime.now()
                
                # Trip circuit breaker if too many failures
                if backend["failures"] >= MAX_FAILURES:
                    logger.warning(f"Disabling backend '{backend_name}' due to {backend['failures']} consecutive failures")
                    backend["enabled"] = False
            else:
                # Reset failure count on success
                backend["failures"] = 0
            
            break

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint with backend status"""
    resources = get_resource_usage()
    
    # Check all backends
    backends_status = []
    for backend in BACKENDS:
        try:
            health_url = backend["url"].replace('/api/tts', '/health')
            response = requests.get(health_url, timeout=5)
            status = "healthy" if response.status_code == 200 else f"unhealthy ({response.status_code})"
        except requests.exceptions.RequestException:
            status = "unreachable"
        
        # Check if circuit breaker should be reset
        check_circuit_breaker(backend)
        
        backends_status.append({
            "name": backend["name"],
            "url": backend["url"],
            "status": status,
            "enabled": backend["enabled"],
            "failures": backend["failures"],
            "success_rate": round(backend["success_rate"], 2),
            "avg_response_time": round(backend["avg_response_time"], 2),
            "requests": backend["requests"]
        })
    
    return jsonify({
        "status": "healthy",
        "backends": backends_status,
        "audio_dir": AUDIO_DIR,
        "cache_dir": CACHE_DIR,
        "resources": resources
    })

@app.route('/tts', methods=['GET', 'POST'])
def tts_endpoint():
    """
    Multi-backend TTS endpoint
    Tries each backend in order of preference, with fallback
    """
    start_time = time.time()
    
    # Get text from request (either GET param or POST data)
    text = None
    
    if request.method == 'GET':
        text = request.args.get('text', '')
    else:
        text = request.form.get('text', '')
        
        # If no form data, try JSON
        if not text and request.is_json:
            text = request.json.get('text', '')
    
    if not text:
        logger.warning("Request received with no text")
        return jsonify({"error": "No text provided"}), 400
    
    # Check resources before proceeding
    resources = get_resource_usage()
    if resources["high_load"]:
        logger.warning(f"System under high load: CPU {resources['cpu_percent']}%, Memory {resources['memory_percent']}%")
    
    # Log request
    request_id = hashlib.md5((text + str(time.time())).encode()).hexdigest()[:8]
    logger.info(f"[{request_id}] TTS request: '{text[:50]}...' ({len(text)} chars)")
    
    # Check cache
    cache_file = get_cache_path(text)
    if os.path.exists(cache_file):
        logger.info(f"[{request_id}] Cache hit for text: '{text[:30]}...' - using {cache_file}")
        return jsonify({
            "status": "success",
            "file": cache_file,
            "cached": True,
            "time": f"{time.time() - start_time:.3f}s"
        })
    
    try:
        # Create a unique filename for this request
        with tempfile.NamedTemporaryFile(suffix='.wav', dir=AUDIO_DIR, delete=False) as temp:
            temp_wav = temp.name
        
        # Select initial backend
        backend = select_backend(len(text))
        if not backend:
            return jsonify({"error": "No TTS backends available"}), 500
        
        success = False
        used_backend = None
        
        # Try each backend until one succeeds
        for attempt in range(len(BACKENDS)):
            if not backend:
                logger.error(f"[{request_id}] No more backends available after {attempt} attempts")
                break
            
            backend_name = backend["name"]
            logger.info(f"[{request_id}] Trying backend '{backend_name}'")
            
            try:
                # Prepare the request
                request_data = json.dumps({"text": text})
                headers = {'Content-Type': 'application/json'}
                
                # Make the request
                backend_start = time.time()
                response = requests.post(
                    backend["url"],
                    data=request_data,
                    headers=headers,
                    stream=True,
                    timeout=backend["timeout"]
                )
                
                if response.status_code != 200:
                    logger.warning(f"[{request_id}] Backend '{backend_name}' returned status {response.status_code}")
                    update_backend_stats(backend_name, False, time.time() - backend_start)
                    # Try next backend
                    backend = select_backend(len(text))
                    continue
                
                # Save the audio file
                with open(temp_wav, 'wb') as f:
                    for chunk in response.iter_content(8192):
                        f.write(chunk)
                
                backend_time = time.time() - backend_start
                logger.info(f"[{request_id}] Backend '{backend_name}' succeeded in {backend_time:.2f}s")
                update_backend_stats(backend_name, True, backend_time)
                
                success = True
                used_backend = backend_name
                break
                
            except requests.exceptions.RequestException as e:
                logger.warning(f"[{request_id}] Backend '{backend_name}' failed: {str(e)}")
                update_backend_stats(backend_name, False, time.time() - backend_start)
                # Try next backend
                backend = select_backend(len(text))
        
        if not success:
            return jsonify({"error": "All TTS backends failed"}), 500
        
        # Convert to Asterisk format
        converted_filename = cache_file
        if not convert_audio(temp_wav, converted_filename, SAMPLE_RATE):
            return jsonify({"error": "Failed to convert audio"}), 500
        
        # Clean up the temporary file
        os.unlink(temp_wav)
        
        # Log successful synthesis
        total_time = time.time() - start_time
        logger.info(f"[{request_id}] Total synthesis time: {total_time:.3f}s using backend '{used_backend}'")
        
        # Return the path for Asterisk to use
        return jsonify({
            "status": "success",
            "file": converted_filename,
            "cached": False,
            "backend": used_backend,
            "time": f"{total_time:.3f}s"
        })
        
    except Exception as e:
        logger.error(f"[{request_id}] Error in TTS process: {str(e)}", exc_info=True)
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    logger.info("Starting Multi-Backend TTS HTTP bridge server on port 5003")
    logger.info(f"Available backends:")
    for backend in BACKENDS:
        logger.info(f"  - {backend['name']}: {backend['url']} (timeout: {backend['timeout']}s, weight: {backend['weight']})")
    app.run(host="0.0.0.0", port=5003)