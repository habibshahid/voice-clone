#!/usr/bin/env python3
"""
Interactive model downloader for Coqui TTS
This script handles the license acknowledgment and downloads the model
"""
import os
import sys
from TTS.api import TTS

print("=== Interactive Model Downloader for Coqui TTS ===")
print("This script will download the XTTS v2 model with license acknowledgment")
print()

# Set environment variable to auto-accept the license
os.environ["COQUI_TOS_AGREED"] = "1"

print("Downloading and initializing the model...")
try:
    # Initialize TTS with the model - this will trigger the download
    tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2")
    
    print("Model downloaded and initialized successfully!")
    print("You can now run the multi-voice system without prompt interruptions.")
    sys.exit(0)
    
except Exception as e:
    print(f"Error downloading model: {str(e)}")
    print("\nAlternative method: Try running the following command interactively:")
    print("python3 -c 'from TTS.api import TTS; TTS(\"tts_models/multilingual/multi-dataset/xtts_v2\")'")
    sys.exit(1)
