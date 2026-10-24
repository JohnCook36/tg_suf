"use strict";

const el = (id) => document.getElementById(id);
let snapshot = null;
let speaking = false;
let token = 0;
let offset = 0;
let saveJob = Promise.resolve();
let lastSavedAt = 0;

function progress() { return snapshot?.progress; }
function current() { return snapshot?.items[progress()?.ordinal] || null; }
function feedback(text) { el("feedback").textContent = text || ""; }

function render() {
  const cp = progress();
  const item = current();
  const total = snapshot?.items.length || 0;
  el("counter").textContent = `${Math.min((cp?.ordinal ?? 0) + 1, total)} / ${total}`;
  el("total").textContent = `${total} публикаций`;
  el("channel").textContent = item ? item.frozen_title : (cp ? "Очередь завершена" : "Нет очереди");
  el("player-title").textContent = item ? `Новость № ${cp.ordinal + 1}` : (cp ? "Всё прослушано" : "Ваши новости, без прокрутки");
  el("post-text").textContent = item ? item.frozen_text : "Создай демонстрационную очередь или синхронизируй Telegram-каналы через консоль.";
  el("position").textContent = item ? `Символ ${offset} из ${item.frozen_text.length} · автосохранение` : "Готово";
  for (const key of ["play", "next", "previous"]) el(key).disabled = !item;
  el("play").innerHTML = speaking ? "Ⅱ <span>Pause</span>" : "▶ <span>Play</span>";
  el("play").setAttribute("aria-label", speaking ? "Пауза" : "Начать чтение");
  el("queue-list").replaceChildren(...(snapshot?.items || []).map((entry, i) => {
    const li = document.createElement("li");
    if (i < (cp?.ordinal || 0)) li.className = "done";
    if (i === (cp?.ordinal || 0)) li.className = "current";
    const number = document.createElement("span");
    number.className = "idx";
    number.textContent = String(i + 1).padStart(2, "0");
    const details = document.createElement("div");
    const title = document.createElement("span");
    title.className = "name";
    title.textContent = entry.frozen_title;
    const preview = document.createElement("span");
    preview.className = "preview";
    preview.textContent = entry.frozen_text;
    details.append(title, preview);
    li.append(number, details);
    return li;
  }));
}

async function load() {
  const response = await fetch("/api/state", {cache:"no-store"});
  if (!response.ok) throw new Error("Не удалось получить очередь");
  snapshot = await response.json();
  offset = progress()?.char_offset || 0;
  render();
}

// Serialize checkpoint writes so optimistic revision is never reused accidentally.
function commit(action, at) {
  const operation = saveJob.then(async () => {
    const cp = progress();
    if (!cp || !current()) return;
    const response = await fetch("/api/checkpoint", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({run_id:cp.run_id, ordinal:cp.ordinal,
        char_offset:at, action, expected_revision:cp.revision})
    });
    if (!response.ok) {
      if (response.status === 409) await load();
      throw new Error(response.status === 409 ? "Позиция изменилась — обнови страницу" : "Не удалось сохранить позицию");
    }
    snapshot = await response.json();
    if (action === "complete" || action === "skip") offset = 0;
    lastSavedAt = Date.now();
    render();
  });
  saveJob = operation.catch((error) => { feedback(error.message); });
  return operation;
}

function stopVoice() {
  token++;
  speaking = false;
  if ("speechSynthesis" in window) speechSynthesis.cancel();
  render();
}

async function pause() {
  if (!speaking) return;
  stopVoice();
  try { await commit("pause", offset); } catch { /* already displayed */ }
}

function chooseVoice() {
  return speechSynthesis.getVoices().find(v => /^ru(-|$)/i.test(v.lang)) || null;
}

async function play() {
  if (speaking) return pause();
  if (!("speechSynthesis" in window)) return feedback("В браузере нет синтеза речи.");
  try {
    await saveJob;
    const item = current();
    if (!item) return;
    if (offset >= item.frozen_text.length) offset = 0;
    await commit("playing", offset);
    const voice = chooseVoice();
    if (!voice) feedback("Русский голос не найден; будет использован голос браузера.");
    else feedback("");
    speaking = true;
    const id = ++token;
    const base = offset;
    const utterance = new SpeechSynthesisUtterance(item.frozen_text.slice(base));
    utterance.lang = "ru-RU";
    if (voice) utterance.voice = voice;
    utterance.rate = Number(el("rate").value);
    utterance.onboundary = (event) => {
      if (id !== token || !speaking) return;
      if (typeof event.charIndex === "number") offset = base + event.charIndex;
      el("position").textContent = `Символ ${offset} из ${item.frozen_text.length} · автосохранение`;
      if (Date.now() - lastSavedAt > 3500) commit("playing", offset).catch(() => stopVoice());
    };
    utterance.onend = async () => {
      if (id !== token || !speaking) return;
      try {
        await commit("complete", item.frozen_text.length);
        if (id !== token || !speaking) return;
        speaking = false;
        render();
        if (current()) await play();
        else feedback("Все новости прослушаны.");
      } catch { stopVoice(); }
    };
    utterance.onerror = (event) => {
      if (id !== token || !speaking) return;
      stopVoice();
      commit("pause", offset).catch(() => {});
      feedback(`Ошибка синтеза речи: ${event.error || "неизвестно"}`);
    };
    render();
    speechSynthesis.speak(utterance);
  } catch (error) {
    stopVoice();
    feedback(error.message);
  }
}

async function next() {
  const shouldContinue = speaking;
  stopVoice();
  try {
    await commit("skip", offset);
    if (shouldContinue && current()) await play();
  } catch (error) { feedback(error.message); }
}

async function fromStart() {
  const shouldContinue = speaking;
  stopVoice();
  offset = 0;
  try {
    await commit("pause", 0);
    if (shouldContinue) await play();
  } catch (error) { feedback(error.message); }
}

el("play").addEventListener("click", play);
el("next").addEventListener("click", next);
el("previous").addEventListener("click", fromStart);
window.addEventListener("pagehide", () => {
  // Best effort: a browser may drop even a keepalive request on sudden process exit.
  if (speaking && current()) {
    const cp = progress();
    fetch("/api/checkpoint", {
      method: "POST", keepalive: true,
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({run_id:cp.run_id, ordinal:cp.ordinal, char_offset:offset,
        action:"pause", expected_revision:cp.revision})
    }).catch(() => {});
  }
});
load().catch(error => feedback(error.message));
