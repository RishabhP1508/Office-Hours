// A form number or status code split across a line break undermines the precision this product
// exists to provide (F-1, H-1B, I-765, I-983, I-129, I-20, E-Verify, ...). This runs over both
// hardcoded UI copy and dynamic text (the echoed question, the model's own answer prose) and
// substitutes a real non-breaking hyphen (U+2011) for the plain ASCII one wherever it sits inside
// one of these tokens, so the browser can never choose that hyphen as a line-break point.

const NON_BREAKING_HYPHEN = "‑";

// Matches:
//   - "E-Verify" (a fixed compound, not a status-code shape)
//   - a status/visa code or form number: 1-3 letters, a hyphen, 1-4 digits, an optional trailing
//     letter -- covers F-1, H-1B, J-1, M-1, I-765, I-983, I-129, I-20, I-515A, and any other
//     form number or status name this corpus introduces later that has the same shape.
const PATTERNS: RegExp[] = [/\bE-Verify\b/gi, /\b[A-Za-z]{1,3}-\d{1,4}[A-Za-z]?\b/g];

export function withNonBreakingHyphens(text: string): string {
  let result = text;
  for (const pattern of PATTERNS) {
    result = result.replace(pattern, (match) => match.replace(/-/g, NON_BREAKING_HYPHEN));
  }
  return result;
}
