import type { TranslateFn } from '../i18n/t';

/**
 * Extract a human-readable message from an axios error.
 *
 * Handles FastAPI's two error-body shapes: a plain `{detail: string}` and a
 * validation `{detail: [{msg, ...}]}` array. Falls back to the provided
 * message. This was duplicated verbatim across the IAM components.
 */
export function extractErrorDetail(err: any, fallback: string): string {
  const detail = err?.response?.data?.detail;
  if (Array.isArray(detail)) {
    return detail.map((d: any) => d?.msg).filter(Boolean).join(', ') || fallback;
  }
  return detail || fallback;
}

const CJK = /[\u4e00-\u9fff]/;

/** Generic copy for the common HTTP classes; business 4xx fall through to `detail`. */
function _statusMessage(status: number): string | undefined {
  if (status === 401) {
    return 'Your session has expired. Please sign in again.';
  }
  if (status === 402) {
    return 'Insufficient balance or quota. Please top up and try again.';
  }
  if (status === 403) {
    return 'You do not have permission to perform this action.';
  }
  if (status === 404) {
    return 'The requested item was not found.';
  }
  if (status === 429) {
    return 'Too many requests. Please slow down and try again.';
  }
  if (status >= 500) {
    return 'The server ran into a problem. Please try again later.';
  }
  return undefined;
}

/**
 * Localized user-facing message for a failed request.
 *
 * Precedence, most specific first:
 *   1. Backend detail already in Chinese (the quota wire messages) - verbatim.
 *   2. A curated translation for this exact backend detail (dictionary entry).
 *   3. Generic copy for the HTTP class, with the raw English detail kept in
 *      parentheses so debugging information is never lost.
 *   4. The caller's fallback, else a generic "something went wrong".
 */
export function localizeError(err: any, t: TranslateFn, fallback?: string): string {
  const status: number | undefined = err?.response?.status;
  const rawDetail = extractErrorDetail(err, '');

  if (rawDetail && CJK.test(rawDetail)) {
    return rawDetail;
  }

  const translatedDetail = rawDetail ? t(rawDetail) : '';
  if (translatedDetail && translatedDetail !== rawDetail) {
    return translatedDetail;
  }

  const genericSource = _statusMessage(status ?? 0) ?? (status ? undefined : 'Network error. Please check your connection and try again.');
  const generic = genericSource ? t(genericSource) : '';
  if (generic) {
    return translatedDetail
      ? t('{message} ({detail})', { message: generic, detail: translatedDetail })
      : generic;
  }
  return translatedDetail || (fallback ? t(fallback) : t('Something went wrong. Please try again.'));
}
