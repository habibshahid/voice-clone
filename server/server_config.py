"""
Custom TTS server configuration for Asterisk integration
"""
import os
import argparse
import json
from flask import Flask, request, send_file
import tempfile
from TTS.utils.synthesizer import Synthesizer

app = Flask(__name__)
synthesizer = None

@app.route('/synthesize', methods=['POST'])
def synthesize_speech():
    """Endpoint for text-to-speech synthesis"""
    if request.method == 'POST':
        if not request.json or 'text' not in request.json:
            return {"error": "No text provided"}, 400
        
        text = request.json['text']
        
        # Create a temporary file to save the audio
        temp_file = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
        temp_filename = temp_file.name
        temp_file.close()
        
        # Synthesize speech
        wav = synthesizer.tts(text=text)
        synthesizer.save_wav(wav, temp_filename)
        
        # Return the file
        return send_file(temp_filename, mimetype='audio/wav')

def main():
    parser = argparse.ArgumentParser(description="TTS Server for Asterisk")
    parser.add_argument("--model_path", required=True, help="Path to the model file")
    parser.add_argument("--config_path", required=True, help="Path to the model config file")
    parser.add_argument("--port", type=int, default=5002, help="Port to run the server on")
    args = parser.parse_args()
    
    global synthesizer
    
    # Initialize synthesizer
    synthesizer = Synthesizer(
        tts_checkpoint=args.model_path,
        tts_config_path=args.config_path,
        use_cuda=False  # Set to True if you have a GPU
    )
    
    # Start server
    app.run(host="0.0.0.0", port=args.port)

if __name__ == "__main__":
    main()
