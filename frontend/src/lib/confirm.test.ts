import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CONFIRM_WINDOW_MS, twoStep } from "./confirm";

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

function setup() {
  const button = document.createElement("button");
  button.className = "btn quiet";
  button.textContent = "trash";
  const action = vi.fn();
  twoStep(button, action);
  return { button, action };
}

describe("twoStep", () => {
  it("asks first and does nothing on a single click", () => {
    const { button, action } = setup();
    button.click();
    expect(button.textContent).toBe("{Delete?}");
    expect(action).not.toHaveBeenCalled();
  });

  it("runs the action on the second click and restores the button", () => {
    const { button, action } = setup();
    button.click();
    button.click();
    expect(action).toHaveBeenCalledTimes(1);
    expect(button.textContent).toBe("trash");
    expect(button.className).toBe("btn quiet");
  });

  it("reverts without acting when nobody confirms", () => {
    const { button, action } = setup();
    button.click();
    vi.advanceTimersByTime(CONFIRM_WINDOW_MS + 1);
    expect(button.textContent).toBe("trash");
    button.click(); // starts over: asks again rather than acting
    expect(button.textContent).toBe("{Delete?}");
    expect(action).not.toHaveBeenCalled();
  });

  it("does not let the click reach other handlers", () => {
    const { button } = setup();
    const outer = vi.fn();
    button.addEventListener("click", outer);
    button.click();
    expect(outer).not.toHaveBeenCalled();
  });
});
