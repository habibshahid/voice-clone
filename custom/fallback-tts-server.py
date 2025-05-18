#!/usr/bin/env python3
"""
Ultra-Lightweight Fallback TTS Server
This server uses a minimal TTS model (Tacotron2) that should work even on very
limited resources. It doesn't do voice cloning but will at least provide a 
functional TTS service for testing and when the primary TTS server fails.
"""
import os
import argparse
import logging
import hashlib
import time
import gc
import warnings
import tempfile
from flask import Flask, request, send_file, jsonify

# Import TTS components
from TTS.utils.manage import ModelManager
from TTS.utils.synthesizer import Synthesizer

# Suppress warnings
warnings.filterwarnings("ignore")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('fallback_tts_server')

app = Flask(__name__)

# Global variables
synthesizer = None
cache_dir = "/tmp/tts-fallback-cache"
voice_name = "ljspeech"  # Default voice
model_name = "tts_models/en/ljspeech/tacotron2-DDC"  # Lightweight model

# Ensure cache directory exists
os.makedirs(cache_dir, exist_ok=True)

def get_cache_path(text):
    """Generate a unique cache path based on text"""
    text_hash = hashlib.md5(text.encode()).hexdigest()
    return os.path.join(cache_dir, f"{text_hash}.wav")

@app.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "model": model_name,
        "voice": voice_name,
        "cache_dir": cache_dir
    })

@app.route("/api/tts", methods=["POST"])
def tts_endpoint():
    """Ultra-lightweight TTS endpoint"""
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
    
    try:
        # Check cache first
        cache_file = get_cache_path(text)
        if os.path.exists(cache_file):
            logger.info(f"[{request_id}] Cache hit for text: '{text[:30]}...' - using {cache_file}")
            return send_file(cache_file, mimetype='audio/wav')
        
        # Log request info
        logger.info(f"[{request_id}] Synthesizing: '{text[:50]}...' ({len(text)} chars)")
        
        # Limit text length to ensure quick processing
        if len(text) > 200:
            logger.warning(f"[{request_id}] Truncating long text from {len(text)} to 200 chars")
            text = text[:200]
        
        # Create a temporary file for the audio
        temp_file = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
        temp_filename = temp_file.name
        temp_file.close()
        
        # Run synthesis
        synthesis_start = time.time()
        wav = synthesizer.tts(text=text)
        synthesizer.save_wav(wav, temp_filename)
        synthesis_time = time.time() - synthesis_start
        
        # Save to cache
        os.rename(temp_filename, cache_file)
        
        # Log completion
        logger.info(f"[{request_id}] Synthesis complete in {synthesis_time:.2f}s - cached as {cache_file}")
        
        # Force garbage collection
        gc.collect()
        
        # Return the audio file
        return send_file(cache_file, mimetype='audio/wav')
        
    except Exception as e:
        logger.error(f"[{request_id}] Error in synthesis: {str(e)}", exc_info=True)
        return {"error": str(e)}, 500

def main():
    global synthesizer, voice_name, model_name
    
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Ultra-Lightweight Fallback TTS Server")
    parser.add_argument("--port", type=int, default=5004, help="Port to run the server on")
    parser.add_argument("--voice", default="ljspeech", help="Voice to use")
    parser.add_argument("--cache_dir", default="/tmp/tts-fallback-cache", help="Cache directory")
    args = parser.parse_args()
    
    # Store settings
    voice_name = args.voice
    cache_dir = args.cache_dir
    
    # Ensure cache directory exists
    os.makedirs(cache_dir, exist_ok=True)
    
    # Set voice-specific model
    if voice_name == "ljspeech":
        model_name = "tts_models/en/ljspeech/tacotron2-DDC"
    elif voice_name == "vctk":
        model_name = "tts_models/en/vctk/vits"
    elif voice_name == "sam":
        model_name = "tts_models/en/sam/tacotron-DDC"
    else:
        model_name = "tts_models/en/ljspeech/tacotron2-DDC"
    
    # Initialize TTS
    try:
        logger.info(f"Loading model manager...")
        model_manager = ModelManager()
        
        logger.info(f"Downloading/loading model: {model_name}")
        model_path, config_path = model_manager.download_model(model_name)
        
        logger.info(f"Initializing synthesizer...")
        synthesizer = Synthesizer(
            tts_checkpoint=model_path,
            tts_config_path=config_path,
            use_cuda=False  # Force CPU for maximum compatibility
        )
        
        logger.info(f"Model loaded successfully")
        logger.info(f"Voice: {voice_name}")
        logger.info(f"Starting server on port {args.port}")
        
    except Exception as e:
        logger.error(f"Error initializing TTS: {str(e)}", exc_info=True)
        return
    
    # Start server
    app.run(host="0.0.0.0", port=args.port, threaded=True)

if __name__ == "__main__":
    main()