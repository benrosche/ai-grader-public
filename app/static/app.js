const setupPanel = document.querySelector("#setup-panel");
const chatPanel = document.querySelector("#chat-panel");
const finalPanel = document.querySelector("#final-panel");
const startForm = document.querySelector("#start-form");
const chatForm = document.querySelector("#chat-form");
const transcript = document.querySelector("#transcript");
const statusMessage = document.querySelector("#status-message");
const timer = document.querySelector("#timer");
const replyCount = document.querySelector("#reply-count");
const finishButton = document.querySelector("#finish-button");
const sendButton = document.querySelector("#send-button");
const messageInput = document.querySelector("#message");
const studentName = document.querySelector("#student-name");
const taskTitle = document.querySelector("#task-title");

let sessionId = null;
let startedAt = null;
let studentReplies = 0;
let finished = false;
let finishing = false;
let sending = false;

function showStatus(message, isError = false) {
  statusMessage.textContent = message;
  statusMessage.classList.toggle("error", isError);
  statusMessage.classList.remove("is-hidden");
}

function hideStatus() {
  statusMessage.classList.add("is-hidden");
}

function setBusy(isBusy) {
  sendButton.disabled = isBusy || finished;
  finishButton.disabled = isBusy || finished || !canFinish();
  messageInput.disabled = isBusy || finished;
}

function addTurn(role, text) {
  const row = document.createElement("div");
  row.className = `turn ${role}`;

  const label = document.createElement("span");
  label.className = "turn-label";
  label.textContent = role === "assistant" ? "AI guest" : "You";

  const content = document.createElement("p");
  content.textContent = text;

  row.append(label, content);
  transcript.append(row);
  transcript.scrollTop = transcript.scrollHeight;
}

function elapsedSeconds() {
  if (!startedAt) return 0;
  return Math.floor((Date.now() - startedAt) / 1000);
}

function formatTime(seconds) {
  const minutes = Math.floor(seconds / 60).toString().padStart(2, "0");
  const remaining = (seconds % 60).toString().padStart(2, "0");
  return `${minutes}:${remaining}`;
}

function canFinish() {
  return (
    elapsedSeconds() >= window.APP_CONFIG.minChatSeconds &&
    studentReplies >= window.APP_CONFIG.minStudentReplies
  );
}

function updateProgress() {
  timer.textContent = formatTime(elapsedSeconds());
  replyCount.textContent = `${studentReplies} / ${window.APP_CONFIG.minStudentReplies} replies`;
  finishButton.disabled = finished || finishing || sending || !canFinish();

  if (!finished && !finishing && !sending && canFinish()) {
    finishConversation();
  }
}

async function readJson(response) {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || "The request failed.");
  }
  return payload;
}

startForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  hideStatus();

  const formData = new FormData(startForm);
  const startButton = startForm.querySelector("button");
  startButton.disabled = true;
  showStatus("Uploading task file and starting the conversation...");

  try {
    const response = await fetch("/api/start", {
      method: "POST",
      body: formData
    });
    const payload = await readJson(response);

    sessionId = payload.session_id;
    startedAt = Date.now();
    studentReplies = 0;
    finished = false;
    finishing = false;
    sending = false;
    studentName.textContent = payload.student_name;
    taskTitle.textContent = payload.task_title || "Task conversation";
    window.APP_CONFIG.minChatSeconds = payload.min_chat_seconds;
    window.APP_CONFIG.minStudentReplies = payload.min_student_replies;

    setupPanel.classList.add("is-hidden");
    chatPanel.classList.remove("is-hidden");
    addTurn("assistant", payload.assistant_message);
    hideStatus();
    messageInput.focus();
  } catch (error) {
    showStatus(error.message, true);
    startButton.disabled = false;
  }
});

chatForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!sessionId || finished || finishing || sending) return;

  const message = messageInput.value.trim();
  if (!message) return;

  addTurn("student", message);
  studentReplies += 1;
  messageInput.value = "";
  sending = true;
  setBusy(true);
  showStatus("Sending response...");

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({session_id: sessionId, message})
    });
    const payload = await readJson(response);

    if (payload.assistant_message) {
      addTurn("assistant", payload.assistant_message);
    }
    if (payload.completed) {
      sending = false;
      completeUi();
    } else {
      sending = false;
      hideStatus();
      setBusy(false);
    }
  } catch (error) {
    sending = false;
    showStatus(error.message, true);
    setBusy(false);
  }
});

messageInput.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
  event.preventDefault();
  if (typeof chatForm.requestSubmit === "function") {
    chatForm.requestSubmit();
  } else {
    chatForm.dispatchEvent(new Event("submit", {cancelable: true}));
  }
});

finishButton.addEventListener("click", () => {
  finishConversation();
});

async function finishConversation() {
  if (!sessionId || finished || finishing || !canFinish()) return;

  finishing = true;
  setBusy(true);
  showStatus("Saving advisory marks...");

  try {
    const response = await fetch("/api/finish", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({session_id: sessionId})
    });
    const payload = await readJson(response);
    if (payload.assistant_message) {
      addTurn("assistant", payload.assistant_message);
    }
    completeUi();
  } catch (error) {
    finishing = false;
    showStatus(error.message, true);
    setBusy(false);
  }
}

function completeUi() {
  finished = true;
  finishing = false;
  chatPanel.classList.add("is-hidden");
  finalPanel.classList.remove("is-hidden");
  hideStatus();
}

setInterval(updateProgress, 1000);
