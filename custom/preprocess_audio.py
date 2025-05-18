#!/usr/bin/env python3
"""
Preprocessing script for voice samples before cloning
"""
import os
import argparse
import glob
import subprocess
import numpy as np
import librosa
from scipy.io import wavfile

def process_audio(input_file, output_file, target_sr=22050):
    """
    Process a single audio file:
    1. Convert to WAV if not already
    2. Resample to target sample rate
    3. Remove silence
    4. Normalize volume
    """
    print(f"Processing {input_file}...")
    
    # Load audio
    try:
        y, sr = librosa.load(input_file, sr=None)
    except Exception as e:
        print(f"Error loading {input_file}: {e}")
        return False
    
    # Resample if needed
    if sr != target_sr:
        y = librosa.resample(y, orig_sr=sr, target_sr=target_sr)
    
    # Trim silence
    y, _ = librosa.effects.trim(y, top_db=30)
    
    # Normalize audio
    y = librosa.util.normalize(y)
    
    # Save processed audio
    try:
        wavfile.write(output_file, target_sr, (y * 32767).astype(np.int16))
        return True
    except Exception as e:
        print(f"Error saving {output_file}: {e}")
        return False

def main():
    parser = argparse.ArgumentParser(description="Preprocess audio files for voice cloning")
    parser.add_argument("--input_dir", required=True, help="Directory with raw voice samples")
    parser.add_argument("--output_dir", required=True, help="Directory to save processed samples")
    parser.add_argument("--sample_rate", type=int, default=22050, help="Target sample rate")
    args = parser.parse_args()
    
    # Create output directory if it doesn't exist
    os.makedirs(args.output_dir, exist_ok=True)
    
    # Get all audio files
    audio_files = []
    for ext in ['*.wav', '*.mp3', '*.m4a', '*.flac', '*.ogg']:
        audio_files.extend(glob.glob(os.path.join(args.input_dir, ext)))
    
    if not audio_files:
        print(f"No audio files found in {args.input_dir}")
        return
    
    print(f"Found {len(audio_files)} audio files")
    
    # Process each file
    success_count = 0
    for audio_file in audio_files:
        basename = os.path.splitext(os.path.basename(audio_file))[0]
        output_file = os.path.join(args.output_dir, f"{basename}.wav")
        
        if process_audio(audio_file, output_file, args.sample_rate):
            success_count += 1
    
    print(f"Successfully processed {success_count} of {len(audio_files)} files")
    print(f"Processed files saved to {args.output_dir}")

if __name__ == "__main__":
    main()