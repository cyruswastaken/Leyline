/**
 * A two-step destructive action without a native dialog: the first click turns
 * the button into a "{Delete?}" confirmation; a second click within a few
 * seconds runs the action, otherwise the button quietly reverts.
 */
export const CONFIRM_WINDOW_MS = 3500;

export function twoStep(
  button: HTMLButtonElement,
  onConfirm: () => void | Promise<void>,
  prompt = "{Delete?}",
): void {
  let timer: number | null = null;
  let original: { className: string; nodes: ChildNode[] } | null = null;

  const revert = (): void => {
    if (timer !== null) window.clearTimeout(timer);
    timer = null;
    if (original) {
      button.className = original.className;
      button.replaceChildren(...original.nodes);
      original = null;
    }
  };

  button.addEventListener("click", (event) => {
    event.stopImmediatePropagation();
    if (timer === null) {
      original = { className: button.className, nodes: [...button.childNodes] };
      button.className = "btn btn-danger btn-sm";
      button.textContent = prompt;
      timer = window.setTimeout(revert, CONFIRM_WINDOW_MS);
      return;
    }
    revert();
    void onConfirm();
  });
}
