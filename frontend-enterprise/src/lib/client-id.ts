/**
 * 生成浏览器侧命令和幂等请求使用的 UUID v4。
 *
 * `crypto.randomUUID()` 只在安全上下文和较新的浏览器中存在；局域网 HTTP 页面可能只有
 * `crypto.getRandomValues()`。优先使用原生 UUID，否则用密码学随机字节组装标准 UUID；仅在
 * Web Crypto 完全不可用的旧环境中使用时间与伪随机数组合作为兼容兜底。
 */
export function createClientId(): string {
  const cryptoApi = globalThis.crypto;
  if (typeof cryptoApi?.randomUUID === 'function') return cryptoApi.randomUUID();

  const bytes = new Uint8Array(16);
  if (typeof cryptoApi?.getRandomValues === 'function') {
    cryptoApi.getRandomValues(bytes);
  } else {
    const timestamp = Date.now();
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256) ^ ((timestamp >> (index % 6)) & 0xff);
    }
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
