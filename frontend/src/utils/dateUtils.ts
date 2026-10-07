import { formatDistanceToNow, parseISO, isValid } from 'date-fns';
import { zhCN } from 'date-fns/locale';
import { translate, type Locale } from '../i18n/t';

/**
 * Format a date string or Date object as relative time (e.g., "2 hours ago", "3 days ago").
 *
 * @param date - ISO 8601 date string or Date object
 * @param locale - console language; defaults to en so non-i18n callers are unaffected
 * @returns Formatted relative time string, or "Unknown" if invalid
 */
export function formatRelativeTime(date: string | Date | null | undefined, locale: Locale = 'en'): string {
  if (!date) {
    return translate(locale, 'Unknown');
  }

  try {
    const dateObj = typeof date === 'string' ? parseISO(date) : date;

    if (!isValid(dateObj)) {
      return translate(locale, 'Unknown');
    }

    return formatDistanceToNow(dateObj, {
      addSuffix: true,
      locale: locale === 'zh' ? zhCN : undefined,
    });
  } catch (error) {
    console.error('Error formatting relative time:', error);
    return translate(locale, 'Unknown');
  }
}

/**
 * Format a date string or Date object as absolute date (e.g., "Jan 15, 2025, 3:30 PM").
 *
 * @param date - ISO 8601 date string or Date object
 * @param locale - console language; defaults to en so non-i18n callers are unaffected
 * @returns Formatted absolute date string, or "Unknown" if invalid
 */
export function formatAbsoluteDate(date: string | Date | null | undefined, locale: Locale = 'en'): string {
  if (!date) {
    return translate(locale, 'Unknown');
  }

  try {
    const dateObj = typeof date === 'string' ? parseISO(date) : date;

    if (!isValid(dateObj)) {
      return translate(locale, 'Unknown');
    }

    return new Intl.DateTimeFormat(locale === 'zh' ? 'zh-CN' : 'en-US', {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    }).format(dateObj);
  } catch (error) {
    console.error('Error formatting absolute date:', error);
    return translate(locale, 'Unknown');
  }
}

/**
 * Format a date with both relative and absolute time for tooltips.
 *
 * @param date - ISO 8601 date string or Date object
 * @param locale - console language; defaults to en so non-i18n callers are unaffected
 * @returns Object with relative and absolute formatted dates
 */
export function formatDateWithTooltip(
  date: string | Date | null | undefined,
  locale: Locale = 'en',
): {
  relative: string;
  absolute: string;
} {
  return {
    relative: formatRelativeTime(date, locale),
    absolute: formatAbsoluteDate(date, locale),
  };
}

/**
 * Format a timestamp as a compact "time since" label (e.g., "2d ago", "5m ago").
 *
 * This is the terse footer format the entity cards use for health-check
 * timestamps. It differs from formatRelativeTime (date-fns long form like
 * "about 2 hours ago") by emitting short unit suffixes. Returns null for
 * missing or invalid timestamps so callers can omit the row entirely.
 *
 * @param timestamp - ISO 8601 date string
 * @param locale - console language; defaults to en so non-i18n callers are unaffected
 * @returns Compact relative label, or null if absent/invalid
 */
export function formatTimeSince(
  timestamp: string | null | undefined,
  locale: Locale = 'en',
): string | null {
  if (!timestamp) {
    return null;
  }

  try {
    const now = new Date();
    const lastChecked = new Date(timestamp);

    if (isNaN(lastChecked.getTime())) {
      return null;
    }

    const diffMs = now.getTime() - lastChecked.getTime();
    const diffSeconds = Math.floor(diffMs / 1000);
    const diffMinutes = Math.floor(diffSeconds / 60);
    const diffHours = Math.floor(diffMinutes / 60);
    const diffDays = Math.floor(diffHours / 24);

    if (diffSeconds < 0) {
      return translate(locale, 'just now');
    } else if (diffDays > 0) {
      return translate(locale, '{count}d ago', { count: diffDays });
    } else if (diffHours > 0) {
      return translate(locale, '{count}h ago', { count: diffHours });
    } else if (diffMinutes > 0) {
      return translate(locale, '{count}m ago', { count: diffMinutes });
    } else {
      return translate(locale, '{count}s ago', { count: diffSeconds });
    }
  } catch (error) {
    console.error('formatTimeSince error:', error, 'for timestamp:', timestamp);
    return null;
  }
}
