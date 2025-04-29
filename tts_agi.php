<?php
/**
 * PHP AGI Script for TTS integration with Asterisk
 * Place this in /var/lib/asterisk/agi-bin/
 */

// Setup AGI environment
require_once 'phpagi.php'; // Make sure you have phpagi installed
$agi = new AGI();
$agi->verbose("TTS PHP AGI script started");

// Get text to synthesize from AGI arguments
$text = $agi->get_variable("TEXT");
$text = $text['data'];

if (empty($text)) {
    $agi->verbose("No text provided");
    $agi->set_variable("TTSSTATUS", "FAILED");
    exit(1);
}

$agi->verbose("Synthesizing text: $text");

// Request speech synthesis from TTS server
try {
    // Initialize cURL session
    $ch = curl_init("http://localhost:5003/tts");
    
    // Setup request
    $data = json_encode(['text' => $text]);
    curl_setopt($ch, CURLOPT_CUSTOMREQUEST, "POST");
    curl_setopt($ch, CURLOPT_POSTFIELDS, $data);
    curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
    curl_setopt($ch, CURLOPT_HTTPHEADER, [
        'Content-Type: application/json',
        'Content-Length: ' . strlen($data)
    ]);
    
    // Execute request
    $response = curl_exec($ch);
    $status = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);
    
    if ($status != 200) {
        $agi->verbose("TTS server error: $status - $response");
        $agi->set_variable("TTSSTATUS", "FAILED");
        exit(1);
    }
    
    // Parse the response
    $result = json_decode($response, true);
    
    if (isset($result['error'])) {
        $agi->verbose("TTS server error: " . $result['error']);
        $agi->set_variable("TTSSTATUS", "FAILED");
        exit(1);
    }
    
    // Get the path to the audio file
    $tts_file = $result['file'];
    
    // Set variable for the dialplan
    $agi->set_variable("TTSSTATUS", "SUCCESS");
    $agi->set_variable("TTSFILE", $tts_file);
    
    $agi->verbose("Speech synthesized successfully: $tts_file");
    
} catch (Exception $e) {
    $agi->verbose("Error in TTS process: " . $e->getMessage());
    $agi->set_variable("TTSSTATUS", "FAILED");
    exit(1);
}
