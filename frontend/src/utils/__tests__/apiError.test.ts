import { extractErrorDetail, localizeError } from '../apiError';
import { translate, type TranslateFn } from '../../i18n/t';

const zh: TranslateFn = (source, vars, context) => translate('zh', source, vars, context);
const en: TranslateFn = (source, vars, context) => translate('en', source, vars, context);

/** axios-shaped fixtures. */
const httpError = (status: number, detail?: unknown) => ({
  response: { status, data: detail === undefined ? {} : { detail } },
});

describe('extractErrorDetail', () => {
  it('returns a plain string detail', () => {
    const err = { response: { data: { detail: 'nope' } } };
    expect(extractErrorDetail(err, 'fallback')).toBe('nope');
  });

  it('joins validation-array detail msgs', () => {
    const err = {
      response: { data: { detail: [{ msg: 'bad a' }, { msg: 'bad b' }] } },
    };
    expect(extractErrorDetail(err, 'fallback')).toBe('bad a, bad b');
  });

  it('falls back when detail is absent', () => {
    expect(extractErrorDetail(new Error('x'), 'fallback')).toBe('fallback');
  });

  it('falls back when the validation array has no usable msgs', () => {
    const err = { response: { data: { detail: [{}, {}] } } };
    expect(extractErrorDetail(err, 'fallback')).toBe('fallback');
  });
});

describe('localizeError', () => {
  it('maps a network failure to localized generic copy', () => {
    expect(localizeError(new Error('Network Error'), zh)).toBe('网络错误，请检查网络连接后重试。');
    expect(localizeError(new Error('Network Error'), en)).toBe('Network error. Please check your connection and try again.');
  });

  it.each([
    [401, '登录状态已过期，请重新登录。'],
    [402, '余额或额度不足，请充值后重试。'],
    [403, '你没有执行该操作的权限。'],
    [404, '请求的内容不存在。'],
    [429, '请求过于频繁，请稍后再试。'],
    [500, '服务端出现问题，请稍后重试。'],
    [503, '服务端出现问题，请稍后重试。'],
  ])('maps status %i to generic Chinese copy', (status, expected) => {
    expect(localizeError(httpError(status), zh)).toBe(expected);
  });

  it('prefers a curated translation of the exact backend detail', () => {
    expect(localizeError(httpError(404, 'Server not found'), zh)).toBe('Server 不存在');
    expect(localizeError(httpError(403, 'You do not have access to this skill'), zh)).toBe('你无权访问该技能');
  });

  it('keeps the raw English detail next to the generic line when untranslated', () => {
    expect(localizeError(httpError(400, 'Parameter region is invalid'), zh)).toBe('Parameter region is invalid');
    expect(localizeError(httpError(503, 'upstream connect error'), zh)).toBe(
      '服务端出现问题，请稍后重试。（upstream connect error）',
    );
  });

  it('passes through backend messages that are already Chinese', () => {
    const quota = '额度不足：该账户的余额或消费窗额度已耗尽，请充值后重试（如认为有误请联系平台管理员）';
    expect(localizeError(httpError(402, quota), zh)).toBe(quota);
  });

  it('falls back to the caller message and then to a generic line', () => {
    expect(localizeError(httpError(400), zh, 'Frobnicate failed')).toBe('Frobnicate failed');
    expect(localizeError(httpError(400), zh)).toBe('出错了，请重试。');
    expect(localizeError(httpError(400), en, 'Frobnicate failed')).toBe('Frobnicate failed');
  });
});
