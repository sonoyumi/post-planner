// Small helpers; every page works without JavaScript.
document.addEventListener("submit", (event) => {
  const form = event.target;
  const button = event.submitter;
  if (button && button.dataset.confirm && !confirm(button.dataset.confirm)) {
    event.preventDefault();
    return;
  }
  if (form.dataset.sent) {  // a double click must not send the form twice
    event.preventDefault();
    return;
  }
  form.dataset.sent = "1";
  setTimeout(() => form.querySelectorAll("button").forEach((b) => (b.disabled = true)), 0);
});

// Length as Telegram counts it (UTF-16 units, like JavaScript's .length), markup characters excluded roughly.
const text = document.getElementById("text");
const counter = document.getElementById("counter");
if (text && counter) {
  const limit = Number(counter.dataset.limit);
  const update = () => {
    const visible = text.value.replace(/\*\*|~~|\|\||`/g, "").replace(/\[([^\]]+)\]\([^)]+\)/g, "$1");
    counter.textContent = `≈ ${visible.length} / ${limit} characters`;
    counter.classList.toggle("bad", visible.length > limit);
  };
  text.addEventListener("input", update);
  update();
}

const use = document.getElementById("use-suggestion");
if (use && text) {
  use.addEventListener("click", () => {
    text.value = document.getElementById("suggestion").textContent;
    text.dispatchEvent(new Event("input"));
    text.focus();
  });
}
