"""
Simple HTTP server to bridge between Asterisk (via PHP AGI) and the Docker TTS service
This runs on the host machine, not in the Docker container
"""
import os
import subprocess
import tempfile
import requests
from flask import Flask, request, send_file, jsonify

app = Flask(__name__)
TTS_DOCKER_URL = "http://localhost:5002/api/tts"
AUDIO_DIR = "/tmp/asterisk-tts"

# Create audio directory if it doesn't exist
os.makedirs(AUDIO_DIR, exist_ok=True)

@app.route('/tts', methods=['GET', 'POST'])
def tts_endpoint():
    """
    Endpoint for text-to-speech synthesis from Asterisk
    Can be called via PHP AGI script using curl
    """
    # Get text from request (either GET param or POST data)
    if request.method == 'GET':
        text = request.args.get('text', '')
    else:
        text = request.form.get('text', '')
        
        # If no form data, try JSON
        if not text and request.is_json:
            text = request.json.get('text', '')
    
    if not text:
        return jsonify({"error": "No text provided"}), 400
    
    # Create a unique filename
    filename = os.path.join(AUDIO_DIR, f"tts_{os.urandom(4).hex()}.wav")
    
    try:
        # Forward request to TTS Docker service
        print(TTS_DOCKER_URL)
        response = requests.post(
            TTS_DOCKER_URL,
            data={"text": text},
            stream=True
        )
        
        if response.status_code != 200:
            return jsonify({"error": f"TTS service error: {response.text}"}), 500
        
        # Save the audio file
        with open(filename, 'wb') as f:
            for chunk in response.iter_content(1024):
                f.write(chunk)
        
        # Convert to format compatible with Asterisk (8kHz mono)
        converted_filename = f"{filename}.converted.wav"
        subprocess.run([
            "sox", 
            filename, 
            "-r", "8000", 
            "-c", "1",
            converted_filename
        ])
        
        # Return the path for Asterisk to use
        return jsonify({
            "status": "success",
            "file": converted_filename
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    print("Starting TTS HTTP server for Asterisk on port 5003")
    app.run(host="0.0.0.0", port=5003)
