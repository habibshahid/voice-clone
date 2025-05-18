#!/usr/bin/env python3
"""
Enhanced Voice Cloning TTS Server with improved performance
Features:
- Better resource management
- Progressive response streaming
- Improved timeout handling
- Memory optimization
- Health monitoring
"""
import os
# Set CUDA environment variables before importing torch
if os.environ.get("USE_GPU", "false").lower() == "true":
    os.environ["CUDA_VISIBLE_DEVICES"] = "0"
else:
    os.environ["CUDA_VISIBLE_DEVICES"] = ""

import argparse
import logging
import hashlib
import json
import time
import gc
import psutil
import threading
import shutil
from flask import Flask, request, send_file, jsonify, Response, stream_with_context
import numpy as np
import torch
import torch.cuda
from TTS.api import TTS
import tempfile
import io

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('voice_clone_server')

app = Flask(__name__)

# Global variables
tts = None
speaker_wav_files = []
language = "en"
language_variant = None 
cache_dir = "/tmp/tts-cache"
model_name = "tts_models/multilingual/multi-dataset/xtts_v2"
use_gpu = False
max_text_length = 300
current_processes = {}
lock = threading.Lock()
voice_name = "default"  # Add voice name for identification

# Performance monitoring
class PerformanceMonitor:
    def __init__(self):
        self.synthesis_times = []
        self.request_lengths = []
        self.max_samples = 100
        
    def add_synthesis_time(self, text_length, time_seconds):
        self.synthesis_times.append(time_seconds)
        self.request_lengths.append(text_length)
        
        # Keep only the most recent samples
        if len(self.synthesis_times) > self.max_samples:
            self.synthesis_times.pop(0)
            self.request_lengths.pop(0)
    
    def get_stats(self):
        if not self.synthesis_times:
            return {
                "avg_time": 0,
                "avg_chars_per_second": 0,
                "max_time": 0,
                "min_time": 0
            }
            
        avg_time = sum(self.synthesis_times) / len(self.synthesis_times)
        avg_text_length = sum(self.request_lengths) / len(self.request_lengths)
        avg_chars_per_second = avg_text_length / avg_time if avg_time > 0 else 0
        
        return {
            "avg_time": round(avg_time, 2),
            "avg_chars_per_second": round(avg_chars_per_second, 2),
            "max_time": round(max(self.synthesis_times), 2),
            "min_time": round(min(self.synthesis_times), 2),
            "samples": len(self.synthesis_times)
        }

perf_monitor = PerformanceMonitor()

def set_process_isolation():
    """Set process isolation to prevent resource conflicts"""
    import resource
    import os
    
    # Set process niceness (lower priority)
    os.nice(10)
    
    # Limit memory usage
    # Get available memory and set soft limit to 1/4 of total
    total_mem = psutil.virtual_memory().total
    soft_limit = int(total_mem / 4)  # Use at most 25% of memory
    hard_limit = soft_limit + (1024 * 1024 * 500)  # Add 500MB buffer for hard limit
    
    # Set memory limits (only works on Linux)
    try:
        resource.setrlimit(resource.RLIMIT_AS, (soft_limit, hard_limit))
        logger.info(f"Set memory limits: soft={soft_limit/(1024*1024)}MB, hard={hard_limit/(1024*1024)}MB")
    except Exception as e:
        logger.warning(f"Could not set memory limits: {e}")
    
    # Set process name for easier identification
    try:
        import setproctitle
        setproctitle.setproctitle(f"tts-voice-{voice_name}")
    except:
        pass
        
def get_cache_path(text, speakers):
    """Generate a unique cache path based on text and speaker files"""
    # Create a unique hash from the text and speaker files
    # Just use filenames rather than full paths for a cleaner hash
    speakers_hash = hashlib.md5(''.join([os.path.basename(s) for s in speakers]).encode()).hexdigest()
    text_hash = hashlib.md5(text.encode()).hexdigest()
    return os.path.join(cache_dir, f"{text_hash}_{speakers_hash}.wav")

def get_resource_usage():
    """Get current resource usage"""
    cpu_percent = psutil.cpu_percent(interval=0.1)
    memory = psutil.virtual_memory()
    if torch.cuda.is_available():
        gpu_memory = {
            i: {
                "total": torch.cuda.get_device_properties(i).total_memory,
                "used": torch.cuda.memory_reserved(i),
                "free": torch.cuda.get_device_properties(i).total_memory - torch.cuda.memory_reserved(i)
            }
            for i in range(torch.cuda.device_count())
        }
    else:
        gpu_memory = {"error": "No CUDA devices available"}
    
    return {
        "cpu_percent": cpu_percent,
        "memory_percent": memory.percent,
        "memory_available_mb": memory.available / (1024 * 1024),
        "active_processes": len(current_processes),
        "gpu": gpu_memory
    }

def cleanup_resources():
    """Force garbage collection and clear CUDA cache if using GPU"""
    gc.collect()
    if use_gpu and torch.cuda.is_available():
        torch.cuda.empty_cache()

@app.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint"""
    resources = get_resource_usage()
    perf_stats = perf_monitor.get_stats()
    
    return jsonify({
        "status": "healthy",
        "model": model_name,
        "gpu_available": torch.cuda.is_available(),
        "gpu_used": use_gpu,
        "speakers": len(speaker_wav_files),
        "language": language,
        "language_variant": language_variant,
        "voice_name": voice_name,  # Include voice name in health check
        "resources": resources,
        "performance": perf_stats,
        "cache_dir": cache_dir,
        "current_processes": len(current_processes)
    })

@app.route("/api/tts", methods=["POST"])
def tts_endpoint():
    """TTS endpoint for voice synthesis with improved performance"""
    start_time = time.time()
    
    # Get text from request
    text = None
    if request.is_json:
        text = request.json.get("text")
    else:
        text = request.form.get("text")
    
    req_language_variant = None
    if request.is_json:
        req_language_variant = request.json.get("language_variant")
    else:
        req_language_variant = request.form.get("language_variant")
        
    if not text:
        logger.warning("Request received with no text")
        return {"error": "No text provided"}, 400
    
    current_language = req_language_variant if req_language_variant else language_variant if language_variant else language

    # Generate a unique ID for this request
    request_id = hashlib.md5((text + str(time.time())).encode()).hexdigest()[:12]
    
    # Track this process
    with lock:
        current_processes[request_id] = {
            "start_time": time.time(),
            "text_length": len(text),
            "status": "starting"
        }
    
    try:
        # Check cache first
        cache_file = get_cache_path(text, speaker_wav_files)
        if os.path.exists(cache_file):
            logger.info(f"[{request_id}] Cache hit for text: '{text[:30]}...' - using {cache_file}")
            with lock:
                if request_id in current_processes:
                    del current_processes[request_id]
            return send_file(cache_file, mimetype='audio/wav')
        
        # Log request info
        logger.info(f"[{request_id}] Synthesizing: '{text[:50]}...' ({len(text)} chars)")
        with lock:
            if request_id in current_processes:
                current_processes[request_id]["status"] = "synthesizing"
        
        # Check if text is too long
        if len(text) > max_text_length:
            logger.warning(f"[{request_id}] Text too long ({len(text)} chars), may cause timeouts")
        
        # Create a temporary file for the audio
        temp_file = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
        temp_filename = temp_file.name
        temp_file.close()
        
        # Synthesize speech using the cloned voice
        # Try to release any memory before synthesis
        cleanup_resources()
        
        # Synthesis
        synthesis_start = time.time()
        tts.tts_to_file(
            text=text,
            file_path=temp_filename,
            speaker_wav=speaker_wav_files,
            language=current_language            
        )
        synthesis_time = time.time() - synthesis_start
        
        # Update performance stats
        perf_monitor.add_synthesis_time(len(text), synthesis_time)
        
        # Save to cache with proper error handling
        cache_dir_path = os.path.dirname(cache_file)
        os.makedirs(cache_dir_path, exist_ok=True)
        
        try:
            os.rename(temp_filename, cache_file)
        except OSError:
            # If rename fails (e.g., cross-device), copy and delete
            shutil.copy2(temp_filename, cache_file)
            os.unlink(temp_filename)
        
        # Log completion
        logger.info(f"[{request_id}] Synthesis complete in {synthesis_time:.2f}s - cached as {cache_file}")
        with lock:
            if request_id in current_processes:
                del current_processes[request_id]
        
        # Clean up resources after synthesis
        cleanup_resources()
        
        # Return the audio file
        return send_file(cache_file, mimetype='audio/wav')
        
    except Exception as e:
        logger.error(f"[{request_id}] Error in synthesis: {str(e)}", exc_info=True)
        with lock:
            if request_id in current_processes:
                del current_processes[request_id]
        cleanup_resources()
        return {"error": str(e)}, 500

@app.route("/api/tts/stream", methods=["POST"])
def tts_stream_endpoint():
    """Streaming TTS endpoint for progressive audio delivery"""
    # Not implemented yet - could be used for future improvements
    # Would allow starting playback before full synthesis is complete
    return jsonify({"error": "Streaming not implemented yet"}), 501

def main():
    global tts, speaker_wav_files, language, model_name, use_gpu, max_text_length, cache_dir, voice_name
    
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Enhanced Voice Cloning TTS Server")
    parser.add_argument("--speaker_wav", nargs='+', required=True, help="Path to speaker WAV file(s)")
    parser.add_argument("--language", default="en", help="Language code")
    parser.add_argument("--language_variant", default=None, help="Specific language variant (e.g., en-gb for British English)")
    parser.add_argument("--port", type=int, default=5002, help="Port to run the server on")
    parser.add_argument("--model", default="tts_models/multilingual/multi-dataset/xtts_v2", help="TTS model to use")
    parser.add_argument("--gpu", action="store_true", help="Use GPU for inference if available")
    parser.add_argument("--cache_dir", default="/tmp/tts-cache", help="Directory for caching generated audio")
    parser.add_argument("--max_text_length", type=int, default=300, help="Warning threshold for text length")
    parser.add_argument("--voice_name", default=None, help="Name of this voice for identification")
    args = parser.parse_args()
    
    #set_process_isolation()
     
    # Store settings
    speaker_wav_files = args.speaker_wav
    language = args.language
    language_variant = args.language_variant
    model_name = args.model
    use_gpu = args.gpu and torch.cuda.is_available()
    cache_dir = args.cache_dir
    max_text_length = args.max_text_length
    
    # Set voice name from command line or derive from first sample file
    if args.voice_name:
        voice_name = args.voice_name
    elif speaker_wav_files and len(speaker_wav_files) > 0:
        # Try to derive voice name from the directory containing the first sample
        sample_dir = os.path.dirname(speaker_wav_files[0])
        voice_name = os.path.basename(sample_dir) if sample_dir else "default"
    
    # Create cache directory if it doesn't exist
    os.makedirs(cache_dir, exist_ok=True)
    
    # Check if WAV files exist
    for wav_file in speaker_wav_files:
        if not os.path.isfile(wav_file):
            logger.error(f"WAV file not found: {wav_file}")
            return
    
    # Initialize TTS with optimized settings
    logger.info(f"Loading model: {model_name}")
    
    # Initialize TTS
    try:
        tts = TTS(model_name, gpu=use_gpu, progress_bar=False)
        
        # Pre-warm the model by synthesizing a short text
        logger.info("Pre-warming the model...")
        _ = tts.tts("This is a test.", speaker_wav=speaker_wav_files[0], language=language)
        cleanup_resources()
        
    except Exception as e:
        logger.error(f"Error loading TTS model: {str(e)}", exc_info=True)
        return
    
    logger.info(f"Model loaded successfully")
    logger.info(f"Using speaker samples: {', '.join(speaker_wav_files)}")
    logger.info(f"Language: {language}")
    logger.info(f"Using GPU: {use_gpu}")
    logger.info(f"Cache directory: {cache_dir}")
    logger.info(f"Voice name: {voice_name}")
    logger.info(f"Starting server on port {args.port}")
    
    # Start server
    app.run(host="0.0.0.0", port=args.port, threaded=True)

if __name__ == "__main__":
    main()