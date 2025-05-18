#!/usr/bin/env python3
"""
Lightweight Voice Cloning TTS Server with Coqui TTS
Features:
- Optimized for low-resource environments
- Progressive response generation
- Lower quality but faster synthesis
- Multi-stage fallback system
"""
import os
import argparse
import logging
import hashlib
import json
import time
import gc
import warnings
import psutil
import threading
import torch
from flask import Flask, request, send_file, jsonify
import tempfile
import io
from TTS.utils.manage import ModelManager
from TTS.utils.synthesizer import Synthesizer

# Suppress warnings
warnings.filterwarnings("ignore")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('lightweight_voice_server')

app = Flask(__name__)

# Global variables
synthesizer = None
speaker_wav_files = []
language = "en"
cache_dir = "/tmp/tts-cache"
use_gpu = False
fallback_enabled = True
current_processes = {}
lock = threading.Lock()

# Ensure cache directory exists
os.makedirs(cache_dir, exist_ok=True)

def get_cache_path(text, speakers):
    """Generate a unique cache path based on text and speaker files"""
    # Create a unique hash from the text and speaker files
    speakers_hash = hashlib.md5(''.join(speakers).encode()).hexdigest()
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
    
    return jsonify({
        "status": "healthy",
        "speakers": len(speaker_wav_files),
        "language": language,
        "resources": resources,
        "cache_dir": cache_dir,
        "current_processes": len(current_processes),
        "fallback_enabled": fallback_enabled
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
    
    if not text:
        logger.warning("Request received with no text")
        return {"error": "No text provided"}, 400
    
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
        
        # Create a temporary file for the audio
        temp_file = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
        temp_filename = temp_file.name
        temp_file.close()
        
        # Synthesize speech using the lightweight approach
        cleanup_resources()
        
        # Try with speaker adaptation first
        try:
            synthesis_start = time.time()
            
            # Limit text length if necessary
            if len(text) > 300:
                logger.warning(f"[{request_id}] Truncating long text from {len(text)} to 300 chars")
                text = text[:300]
            
            # Run synthesis with optimized settings
            with torch.no_grad():
                # Use the speaker_wav parameter to adapt the voice
                wav = synthesizer.tts(
                    text=text,
                    speaker_wav=speaker_wav_files[0],
                    language=language
                )
                synthesizer.save_wav(wav, temp_filename)
            
            synthesis_time = time.time() - synthesis_start
            logger.info(f"[{request_id}] Synthesis complete in {synthesis_time:.2f}s")
            
        except Exception as synthesis_error:
            # Log the synthesis error
            logger.error(f"[{request_id}] Error in primary synthesis: {str(synthesis_error)}")
            
            if not fallback_enabled:
                raise synthesis_error
                
            # Try with fallback: use a simpler model if available
            logger.info(f"[{request_id}] Attempting fallback synthesis")
            
            try:
                # Use a simpler model without voice cloning
                model_manager = ModelManager()
                model_path, config_path = model_manager.download_model("tts_models/en/ljspeech/tacotron2-DDC")
                
                fallback_synthesizer = Synthesizer(
                    tts_checkpoint=model_path,
                    tts_config_path=config_path,
                    use_cuda=use_gpu
                )
                
                wav = fallback_synthesizer.tts(text=text)
                fallback_synthesizer.save_wav(wav, temp_filename)
                
                logger.info(f"[{request_id}] Fallback synthesis succeeded")
            except Exception as fallback_error:
                logger.error(f"[{request_id}] Fallback synthesis also failed: {str(fallback_error)}")
                raise fallback_error
        
        # Save to cache
        os.rename(temp_filename, cache_file)
        
        # Log completion
        total_time = time.time() - start_time
        logger.info(f"[{request_id}] Total processing time: {total_time:.2f}s - cached as {cache_file}")
        
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

def main():
    global synthesizer, speaker_wav_files, language, use_gpu, fallback_enabled
    
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Lightweight Voice Cloning TTS Server")
    parser.add_argument("--speaker_wav", nargs='+', required=True, help="Path to speaker WAV file(s)")
    parser.add_argument("--language", default="en", help="Language code")
    parser.add_argument("--port", type=int, default=5002, help="Port to run the server on")
    parser.add_argument("--model", default="tts_models/multilingual/multi-dataset/xtts_v2", help="TTS model to use")
    parser.add_argument("--gpu", action="store_true", help="Use GPU for inference if available")
    parser.add_argument("--cache_dir", default="/tmp/tts-cache", help="Directory for caching generated audio")
    parser.add_argument("--no-fallback", action="store_true", help="Disable fallback to simpler models")
    args = parser.parse_args()
    
    # Store settings
    speaker_wav_files = args.speaker_wav
    language = args.language
    model_name = args.model
    use_gpu = args.gpu and torch.cuda.is_available()
    cache_dir = args.cache_dir
    fallback_enabled = not args.no_fallback
    
    # Check if WAV files exist
    for wav_file in speaker_wav_files:
        if not os.path.isfile(wav_file):
            logger.error(f"WAV file not found: {wav_file}")
            return
    
    # Set environment variables to optimize TTS loading
    if use_gpu:
        os.environ["CUDA_VISIBLE_DEVICES"] = "0"
    else:
        os.environ["CUDA_VISIBLE_DEVICES"] = ""
    
    # Initialize TTS with manual setup (more lightweight than TTS API)
    try:
        logger.info(f"Loading model manager...")
        model_manager = ModelManager()
        
        logger.info(f"Downloading/loading model: {model_name}")
        model_path, config_path = model_manager.download_model(model_name)
        
        logger.info(f"Initializing synthesizer...")
        synthesizer = Synthesizer(
            tts_checkpoint=model_path,
            tts_config_path=config_path,
            use_cuda=use_gpu
        )
        
        logger.info(f"Model loaded successfully")
        logger.info(f"Using speaker samples: {', '.join(speaker_wav_files)}")
        logger.info(f"Language: {language}")
        logger.info(f"Using GPU: {use_gpu}")
        logger.info(f"Fallback enabled: {fallback_enabled}")
        logger.info(f"Starting server on port {args.port}")
        
    except Exception as e:
        logger.error(f"Error initializing TTS: {str(e)}", exc_info=True)
        return
    
    # Start server with threading enabled
    app.run(host="0.0.0.0", port=args.port, threaded=True)

if __name__ == "__main__":
    main()