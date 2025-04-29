"""
Script to clone a voice using Coqui TTS
"""
import os
import argparse
import json
from glob import glob

def main():
    parser = argparse.ArgumentParser(description="Clone a voice using Coqui TTS")
    parser.add_argument("--samples_dir", required=True, help="Directory with voice samples")
    parser.add_argument("--output_dir", required=True, help="Directory to save the model")
    parser.add_argument("--name", required=True, help="Name for the voice model")
    args = parser.parse_args()
    
    # Check if sample directory exists
    if not os.path.isdir(args.samples_dir):
        print(f"Error: Sample directory {args.samples_dir} does not exist")
        return
    
    # Create output directory if it doesn't exist
    os.makedirs(args.output_dir, exist_ok=True)
    
    # Find WAV files in the samples directory
    wav_files = glob(os.path.join(args.samples_dir, "*.wav"))
    
    if not wav_files:
        print(f"Error: No WAV files found in {args.samples_dir}")
        return
    
    print(f"Found {len(wav_files)} WAV files for voice cloning")
    
    # Create metadata.csv file for training
    metadata_path = os.path.join(args.samples_dir, "metadata.csv")
    with open(metadata_path, "w") as f:
        for wav_file in wav_files:
            # Get the basename of the file
            basename = os.path.basename(wav_file)
            # Write metadata entry (filename without extension|text placeholder)
            f.write(f"{os.path.splitext(basename)[0]}|This is a sample for voice cloning.\n")
    
    print(f"Created metadata file at {metadata_path}")
    
    # Create training command
    # Using YourTTS as it's one of the best models for voice cloning in Coqui TTS
    
    train_cmd = (
        f"python -m TTS.bin.train_tts "
        f"--config_path TTS/tts/configs/yourtts/config.json "
        f"--output_path {args.output_dir} "
        f"--speaker_id_list_path {metadata_path} "
        f"--model_args speaker_encoder_checkpoint_path=null,speaker_encoder_config_path=null"
    )
    
    print("\n===== Voice Cloning Command =====")
    print(train_cmd)
    print("\nRun this command in the Docker container to start voice cloning:")
    print(f"docker exec -it coqui-tts {train_cmd}")
    
    # Create a config file for the server
    config = {
        "model_path": f"{args.output_dir}/best_model.pth",
        "config_path": f"{args.output_dir}/config.json",
        "speakers_file_path": metadata_path,
        "voice_name": args.name
    }
    
    config_path = os.path.join(args.output_dir, "server_config.json")
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
    
    print(f"\nCreated server configuration at {config_path}")
    print("\nAfter training is complete, start the TTS server with:")
    print(f"docker exec -it coqui-tts python /app/server/server_config.py --model_path {config['model_path']} --config_path {config['config_path']} --port 5002")

if __name__ == "__main__":
    main()
