import { describe, expect, it } from 'vitest';

import { firstErrorLine, formatAge, text } from './format';

describe('dashboard format helpers', () => {
  it('formats empty text consistently', () => {
    expect(text(null)).toBe('-');
    expect(text(undefined)).toBe('-');
    expect(text('')).toBe('-');
    expect(text('github_reviewer')).toBe('github_reviewer');
  });

  it('formats task ages compactly', () => {
    expect(formatAge(12)).toBe('12s');
    expect(formatAge(125)).toBe('2m');
    expect(formatAge(3665)).toBe('1h 1m');
    expect(formatAge(90000)).toBe('1d 1h');
  });

  it('extracts first error line', () => {
    expect(firstErrorLine(null)).toBe('');
    expect(firstErrorLine('boom\ntraceback')).toBe('boom');
  });
});
