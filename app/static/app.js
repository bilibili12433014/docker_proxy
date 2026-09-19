document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-copy-target]");
    if (!button) return;
    const target = document.getElementById(button.dataset.copyTarget);
    if (!target) return;
    const value = target.textContent.trim();
    try {
        await navigator.clipboard.writeText(value);
    } catch {
        const area = document.createElement("textarea");
        area.value = value;
        document.body.appendChild(area);
        area.select();
        document.execCommand("copy");
        area.remove();
    }
    const original = button.textContent;
    button.textContent = "已复制";
    setTimeout(() => {
        button.textContent = original;
    }, 1200);
});
