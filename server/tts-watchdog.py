#!/usr/bin/env python3
"""
TTS Service Watchdog
- Monitors the TTS service for responsiveness
- Restarts Docker container if the service becomes unresponsive
- Sends alerts if multiple restarts are needed
- Logs system resource usage
"""
import os
import sys
import time
import logging
import json
import subprocess
import requests
import psutil
import argparse
from datetime import datetime

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('/var/log/tts-watchdog.log')
    ]
)
logger = logging.getLogger('tts_watchdog')

# Default settings
TTS_DOCKER_URL = "http://localhost:5002/health"
BRIDGE_URL = "http://localhost:5003/health"
CHECK_INTERVAL = 60  # seconds
MAX_RESTARTS = 3  # per day
RESTART_COOLDOWN = 300  # seconds
CONTAINER_NAME = "coqui-tts"

# For tracking restarts
restart_count = 0
last_restart_time = 0
restart_dates = {}

def get_system_resources():
    """Get system resource usage information"""
    cpu_percent = psutil.cpu_percent(interval=1)
    memory = psutil.virtual_memory()
    
    # Get disk space for important directories
    disk_usage = {}
    for path in ['/', '/tmp', '/var/log']:
        try:
            usage = psutil.disk_usage(path)
            disk_usage[path] = {
                'total_gb': round(usage.total / (1024**3), 2),
                'used_gb': round(usage.used / (1024**3), 2),
                'free_gb': round(usage.free / (1024**3), 2),
                'percent': usage.percent
            }
        except:
            disk_usage[path] = "unavailable"
    
    # Check system load
    try:
        load_avg = os.getloadavg()
    except:
        load_avg = (0, 0, 0)
    
    return {
        'timestamp': datetime.now().isoformat(),
        'cpu_percent': cpu_percent,
        'memory': {
            'total_gb': round(memory.total / (1024**3), 2),
            'available_gb': round(memory.available / (1024**3), 2),
            'used_percent': memory.percent
        },
        'disk_usage': disk_usage,
        'load_avg': load_avg
    }

def check_tts_service():
    """Check if the TTS service is responsive"""
    try:
        response = requests.get(TTS_DOCKER_URL, timeout=10)
        if response.status_code == 200:
            return True, "TTS service is responsive"
        else:
            return False, f"TTS service returned status code {response.status_code}"
    except requests.exceptions.RequestException as e:
        return False, f"TTS service is unresponsive: {str(e)}"

def check_bridge_service():
    """Check if the bridge service is responsive"""
    try:
        response = requests.get(BRIDGE_URL, timeout=10)
        if response.status_code == 200:
            return True, "Bridge service is responsive"
        else:
            return False, f"Bridge service returned status code {response.status_code}"
    except requests.exceptions.RequestException as e:
        return False, f"Bridge service is unresponsive: {str(e)}"

def restart_tts_container():
    """Restart the TTS Docker container"""
    global restart_count, last_restart_time
    
    # Check if we're within cooldown period
    current_time = time.time()
    if current_time - last_restart_time < RESTART_COOLDOWN:
        logger.warning(f"Skipping restart: within cooldown period ({RESTART_COOLDOWN} seconds)")
        return False, "Skipped restart due to cooldown period"
    
    # Track restart date
    today = datetime.now().strftime('%Y-%m-%d')
    restart_dates[today] = restart_dates.get(today, 0) + 1
    
    # Check if we've exceeded max restarts for today
    if restart_dates[today] > MAX_RESTARTS:
        logger.error(f"Too many restarts today ({restart_dates[today]}), not restarting container")
        return False, f"Exceeded maximum restarts for today ({MAX_RESTARTS})"
    
    logger.info(f"Restarting Docker container: {CONTAINER_NAME}")
    
    try:
        # First try gracefully stopping the container
        stop_cmd = ["docker", "stop", "--time", "30", CONTAINER_NAME]
        subprocess.run(stop_cmd, check=True, capture_output=True)
        
        # Then start it again
        start_cmd = ["docker", "start", CONTAINER_NAME]
        subprocess.run(start_cmd, check=True, capture_output=True)
        
        restart_count += 1
        last_restart_time = current_time
        
        logger.info(f"Container restarted successfully (restart #{restart_count} today)")
        return True, "Container restarted successfully"
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to restart container: {e.stderr.decode()}")
        return False, f"Failed to restart container: {e.stderr.decode()}"

def restart_bridge_service():
    """Restart the TTS bridge service"""
    logger.info("Restarting TTS bridge service")
    
    try:
        # Restart the systemd service
        cmd = ["systemctl", "restart", "tts-http-server"]
        subprocess.run(cmd, check=True, capture_output=True)
        
        logger.info("Bridge service restarted successfully")
        return True, "Bridge service restarted successfully"
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to restart bridge service: {e.stderr.decode()}")
        return False, f"Failed to restart bridge service: {e.stderr.decode()}"

def send_alert(message):
    """Send an alert that the service needed to be restarted"""
    # This function could be expanded to send emails, SMS, etc.
    logger.critical(f"ALERT: {message}")
    
    # Write to a dedicated alert log
    with open('/var/log/tts-alerts.log', 'a') as f:
        timestamp = datetime.now().isoformat()
        f.write(f"{timestamp} - {message}\n")

def main():
    parser = argparse.ArgumentParser(description="TTS Service Watchdog")
    parser.add_argument("--tts-url", default=TTS_DOCKER_URL, help="URL for TTS service health check")
    parser.add_argument("--bridge-url", default=BRIDGE_URL, help="URL for bridge service health check")
    parser.add_argument("--interval", type=int, default=CHECK_INTERVAL, help="Check interval in seconds")
    parser.add_argument("--max-restarts", type=int, default=MAX_RESTARTS, help="Maximum restarts per day")
    parser.add_argument("--container", default=CONTAINER_NAME, help="Docker container name")
    args = parser.parse_args()
    
    # Update global settings
    global TTS_DOCKER_URL, BRIDGE_URL, CHECK_INTERVAL, MAX_RESTARTS, CONTAINER_NAME
    TTS_DOCKER_URL = args.tts_url
    BRIDGE_URL = args.bridge_url
    CHECK_INTERVAL = args.interval
    MAX_RESTARTS = args.max_restarts
    CONTAINER_NAME = args.container
    
    logger.info(f"Starting TTS watchdog service")
    logger.info(f"TTS service URL: {TTS_DOCKER_URL}")
    logger.info(f"Bridge service URL: {BRIDGE_URL}")
    logger.info(f"Check interval: {CHECK_INTERVAL} seconds")
    logger.info(f"Maximum restarts per day: {MAX_RESTARTS}")
    logger.info(f"Container name: {CONTAINER_NAME}")
    
    # Main monitoring loop
    while True:
        try:
            # Log system resources periodically
            resources = get_system_resources()
            logger.info(f"System resources: CPU {resources['cpu_percent']}%, Memory {resources['memory']['used_percent']}%")
            
            # Check TTS service
            tts_ok, tts_message = check_tts_service()
            if not tts_ok:
                logger.warning(f"TTS service check failed: {tts_message}")
                
                # Get more detailed system info before restart
                resources = get_system_resources()
                logger.info(f"System resources before restart: {json.dumps(resources)}")
                
                # Restart the container
                restarted, restart_message = restart_tts_container()
                
                if restarted:
                    # Wait for container to fully start
                    logger.info("Waiting 60 seconds for container to start up...")
                    time.sleep(60)
                    
                    # Check if it's responsive now
                    tts_ok, tts_message = check_tts_service()
                    if not tts_ok:
                        logger.error("TTS service still unresponsive after restart")
                        send_alert(f"TTS service remains unresponsive after restart: {tts_message}")
                    else:
                        logger.info("TTS service is now responsive after restart")
                else:
                    logger.warning(f"Container restart skipped: {restart_message}")
                    
            else:
                logger.info(f"TTS service check passed: {tts_message}")
            
            # Check bridge service
            bridge_ok, bridge_message = check_bridge_service()
            if not bridge_ok:
                logger.warning(f"Bridge service check failed: {bridge_message}")
                
                # Restart the bridge service
                restarted, restart_message = restart_bridge_service()
                
                if restarted:
                    # Wait for service to fully start
                    logger.info("Waiting 10 seconds for bridge service to start up...")
                    time.sleep(10)
                    
                    # Check if it's responsive now
                    bridge_ok, bridge_message = check_bridge_service()
                    if not bridge_ok:
                        logger.error("Bridge service still unresponsive after restart")
                        send_alert(f"Bridge service remains unresponsive after restart: {bridge_message}")
                    else:
                        logger.info("Bridge service is now responsive after restart")
                else:
                    logger.warning(f"Bridge service restart failed: {restart_message}")
                    
            else:
                logger.info(f"Bridge service check passed: {bridge_message}")
                
        except Exception as e:
            logger.error(f"Error in watchdog monitoring: {str(e)}", exc_info=True)
        
        # Wait for next check
        logger.debug(f"Sleeping for {CHECK_INTERVAL} seconds until next check")
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()