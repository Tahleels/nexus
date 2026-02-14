// Global variables
let currentPage = 'dashboard';
let currentChatType = null;
let currentAgentId = null;

// Initialize application
document.addEventListener('DOMContentLoaded', function() {
    initializeEventListeners();
    initializeTooltips();
});

function initializeEventListeners() {
    // Add enter key listener for chat inputs
    const biInput = document.getElementById('bi-message-input');
    const reasoningInput = document.getElementById('reasoning-message-input');
    
    if (biInput) {
        biInput.addEventListener('keypress', function(e) {
            if (e.key === 'Enter') {
                sendMessage('bi');
            }
        });
    }
    
    if (reasoningInput) {
        reasoningInput.addEventListener('keypress', function(e) {
            if (e.key === 'Enter') {
                sendMessage('reasoning');
            }
        });
    }
}

function initializeTooltips() {
    var tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'));
    var tooltipList = tooltipTriggerList.map(function (tooltipTriggerEl) {
        return new bootstrap.Tooltip(tooltipTriggerEl);
    });
}

// Speech to Text JS code
document.addEventListener("DOMContentLoaded", function () {
  // Browser speech API
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    alert("Your browser does not support Speech Recognition API.");
    return;
  }

  const recognition = new SpeechRecognition();
  recognition.continuous = true;     // keep recording until stopped
  recognition.interimResults = true; // show partial results
  recognition.lang = "en-US";

  let finalTranscript = "";
  const chatInput = document.getElementById("chatInput");
  const speechBtn = document.getElementById("speechBtn");
  const transcriptPreview = document.getElementById("speechTranscript");

  let isRecording = false;

  recognition.onresult = (event) => {
    let interimTranscript = "";
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const transcript = event.results[i][0].transcript;
      if (event.results[i].isFinal) {
        finalTranscript += transcript + " ";
      } else {
        interimTranscript += transcript;
      }
    }
    transcriptPreview.textContent = finalTranscript + interimTranscript;
    chatInput.value = finalTranscript + interimTranscript;
  };

  recognition.onstart = () => {
    isRecording = true;
    speechBtn.classList.add("btn-danger");
  };

  recognition.onend = () => {
    isRecording = false;
    speechBtn.classList.remove("btn-danger");
  };

  recognition.onerror = (event) => {
    console.error("Speech recognition error:", event.error);
  };

  // Toggle speech on mic button click
  speechBtn.addEventListener("click", () => {
    if (!isRecording) {
      finalTranscript = "";
      transcriptPreview.textContent = "";
      recognition.start();
    } else {
      recognition.stop();
    }
  });
});
