#!/usr/bin/env python3
"""
Voice Sample Collector and Processor
- Extracts good-quality segments from longer audio files
- Splits audio into optimal-length segments for voice cloning
- Removes segments with background noise, music, or silence
"""
import os
import argparse
import numpy as np
import librosa
import soundfile as sf
from pydub import AudioSegment
from pydub.silence import split_on_silence

def extract_segments(input_file, output_dir, min_length=3.0, max_length=10.0):
    """
    Extract well-formed speech segments from a longer audio file
    """
    print(f"Processing {input_file}...")
    
    # Load audio file using pydub
    try:
        audio = AudioSegment.from_file(input_file)
    except Exception as e:
        print(f"Error loading {input_file}: {e}")
        return 0
    
    # Split on silence
    segments = split_on_silence(
        audio,
        min_silence_len=500,  # minimum silence length in ms
        silence_thresh=-35,   # silence threshold in dB
        keep_silence=300      # keep 300ms of silence at the beginning and end
    )
    
    # Process segments
    segment_count = 0
    for i, segment in enumerate(segments):
        # Skip if segment too short
        duration_sec = len(segment) / 1000.0
        if duration_sec < min_length:
            continue
            
        # Split longer segments
        if duration_sec > max_length:
            subsegments = []
            # Split into subsegments of max_length with 0.5s overlap
            for start in np.arange(0, len(segment), (max_length - 0.5) * 1000):
                end = min(start + max_length * 1000, len(segment))
                subsegments.append(segment[start:end])
                
                # Stop if remaining segment is too short
                if end - start < min_length * 1000:
                    break
        else:
            subsegments = [segment]
        
        # Save segments
        for j, subsegment in enumerate(subsegments):
            # Convert to numpy array for analysis
            samples = np.array(subsegment.get_array_of_samples())
            
            # Skip if average volume is too low (likely silence or background noise)
            if np.abs(samples).mean() < 500:
                continue
                
            # Calculate zero-crossing rate (high for noise, low for clean speech)
            samples_float = samples.astype(float) / 32768.0
            zcr = librosa.feature.zero_crossing_rate(samples_float)[0].mean()
            
            # Skip if zero-crossing rate is too high (likely noise)
            if zcr > 0.15:
                continue
            
            # All checks passed, save the segment
            segment_filename = os.path.join(
                output_dir, 
                f"segment_{i:02d}_{j:03d}.wav"
            )
            subsegment.export(segment_filename, format="wav")
            segment_count += 1
            
    print(f"Extracted {segment_count} segments from {input_file}")
    return segment_count

def main():
    parser = argparse.ArgumentParser(description="Extract voice samples for cloning")
    parser.add_argument("--input", required=True, help="Input audio file or directory")
    parser.add_argument("--output_dir", required=True, help="Directory to save processed segments")
    parser.add_argument("--min_length", type=float, default=3.0, help="Minimum segment length in seconds")
    parser.add_argument("--max_length", type=float, default=10.0, help="Maximum segment length in seconds")
    args = parser.parse_args()
    
    # Create output directory if it doesn't exist
    os.makedirs(args.output_dir, exist_ok=True)
    
    # Process files
    total_segments = 0
    
    if os.path.isdir(args.input):
        # Process all audio files in directory
        for root, _, files in os.walk(args.input):
            for file in files:
                if file.lower().endswith(('.wav', '.mp3', '.flac', '.m4a', '.ogg')):
                    input_file = os.path.join(root, file)
                    total_segments += extract_segments(
                        input_file, 
                        args.output_dir,
                        args.min_length,
                        args.max_length
                    )
    elif os.path.isfile(args.input):
        # Process single file
        total_segments = extract_segments(
            args.input,
            args.output_dir,
            args.min_length,
            args.max_length
        )
    else:
        print(f"Input path not found: {args.input}")
        return
    
    print(f"Total segments extracted: {total_segments}")
    
    if total_segments == 0:
        print("No segments extracted. Try adjusting the parameters.")
    elif total_segments < 3:
        print("Warning: For best results, at least 3-5 good quality segments are recommended.")
    
    # Print next steps
    print("\nNext steps:")
    print(f"1. Review the segments in {args.output_dir}")
    print("2. Delete any segments with poor quality or background noise")
    print("3. Use the remaining segments for voice cloning")

if __name__ == "__main__":
    main()