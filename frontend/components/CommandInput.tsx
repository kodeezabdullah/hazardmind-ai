"use client";

import { ArrowUp } from "lucide-react";
import { useState } from "react";

type CommandInputProps = {
  onSubmit?: (query: string) => void;
  placeholder?: string;
  disabled?: boolean;
};

export function CommandInput({
  onSubmit,
  placeholder = "Ask HazardMind",
  disabled = false,
}: CommandInputProps) {
  const [value, setValue] = useState("");

  const submit = () => {
    const query = value.trim();
    if (!query || disabled) {
      return;
    }
    onSubmit?.(query);
    setValue("");
  };

  return (
    <div className="command-input-dock">
      <div className="command-input-shell">
        <textarea
          className="command-input-field"
          value={value}
          onChange={(event) => setValue(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              submit();
            }
          }}
          placeholder={placeholder}
          rows={1}
          disabled={disabled}
        />
        <button
          type="button"
          className="command-input-send"
          onClick={submit}
          disabled={disabled || value.trim().length === 0}
          aria-label="Send"
        >
          <ArrowUp className="h-5 w-5" />
        </button>
      </div>
    </div>
  );
}
