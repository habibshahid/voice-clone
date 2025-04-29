#!/usr/bin/env python3

import os
import argparse
from flask import Flask, request, send_file
import numpy as np
import torch
from TTS.api import TTS
import tempfile

app = Flask(__name__)

# Global variables
tts = None
speaker_wav_files = []
language = "en"

@app.route("/api/tts", methods=["POST"])
def tts_endpoint():
    """TTS endpoint for voice synthesis"""
    # Get text from request
    text = request.form.get("text")
    if not text:
        return {"error": "No text provided"}, 400
    
    try:
        # Create a temporary file for the audio
        temp_file = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
        temp_filename = temp_file.name
        temp_file.close()
        
        # Synthesize speech using the cloned voice
        tts.tts_to_file(
            text=text,
            file_path=temp_filename,
            speaker_wav=speaker_wav_files,
            language=language
        )
        
        # Return the audio file
        return send_file(temp_filename, mimetype='audio/wav')
        
    except Exception as e:
        return {"error": str(e)}, 500

def main():
    global tts, speaker_wav_files, language
    
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Voice Cloning TTS Server")
    parser.add_argument("--speaker_wav", nargs='+', required=True, help="Path to speaker WAV file(s)")
    parser.add_argument("--language", default="en", help="Language code")
    parser.add_argument("--port", type=int, default=5002, help="Port to run the server on")
    args = parser.parse_args()
    
    # Store settings
    speaker_wav_files = args.speaker_wav
    language = args.language
    
    # Check if WAV files exist
    for wav_file in speaker_wav_files:
        if not os.path.isfile(wav_file):
            print(f"Error: WAV file not found: {wav_file}")
            return
    
    # Initialize TTS
    tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2")
    
    print(f"Loaded XTTS v2 model")
    print(f"Using speaker samples: {', '.join(speaker_wav_files)}")
    print(f"Language: {language}")
    print(f"Starting server on port {args.port}")
    
    # Start server
    app.run(host="0.0.0.0", port=args.port)

if __name__ == "__main__":
    main()