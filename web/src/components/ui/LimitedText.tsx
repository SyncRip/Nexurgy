// S2: text inputs whose maxLength is an entry limit the server defines once (server/app/ai/limits.py), read through
// GET /system/capabilities. Use these instead of a hard-coded maxLength (server/tests/test_input_limits.py checks).

import type { InputHTMLAttributes, Ref, TextareaHTMLAttributes } from 'react';
import { inputLimit, useLimits } from '../../api/limits';
import type { InputLimitKey } from '../../api/types';

type TextareaProps = Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, 'maxLength'> & {
  limit: InputLimitKey;
  ref?: Ref<HTMLTextAreaElement>;
};

export function LimitedTextarea({ limit, ref, ...props }: TextareaProps) {
  const limits = useLimits();
  return <textarea ref={ref} {...props} maxLength={inputLimit(limits, limit)} />;
}

type InputProps = Omit<InputHTMLAttributes<HTMLInputElement>, 'maxLength'> & {
  limit: InputLimitKey;
  ref?: Ref<HTMLInputElement>;
};

export function LimitedInput({ limit, ref, ...props }: InputProps) {
  const limits = useLimits();
  return <input ref={ref} {...props} maxLength={inputLimit(limits, limit)} />;
}
