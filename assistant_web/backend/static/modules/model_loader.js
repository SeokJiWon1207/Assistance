// 초기화와 전송이 같은 재시도를 공유한다. 서버 재기동을 기다리되 무한히 요청하지 않는다.
export function createModelLoader({ fetch, onLoaded, onStatus }) {
  let pending = null;
  let loaded = false;

  async function requestModels() {
    for (let attempt = 0; attempt < 8; attempt++) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 10_000);
      try {
        const response = await fetch('/api/models', { signal: controller.signal, cache: 'no-store' });
        if (!response.ok) {
          const error = new Error(response.status === 401 ? '로그인이 만료되었습니다. 다시 로그인해 주세요.' : '모델 목록을 불러오지 못했습니다.');
          error.retryable = response.status === 408 || response.status === 429 || response.status >= 500;
          throw error;
        }
        const data = await response.json();
        if (!Array.isArray(data.models) || !data.models.length) {
          const error = new Error('사용 가능한 모델이 설정되어 있지 않습니다. 관리자에게 문의해 주세요.');
          error.retryable = false;
          throw error;
        }
        onLoaded(data.models);
        loaded = true;
        return;
      } catch (error) {
        if (error.retryable === false) throw error;
        if (attempt === 7) throw new Error('모델 연결을 복구하지 못했습니다. 잠시 후 다시 전송해 주세요.');
      } finally {
        clearTimeout(timer);
      }
      const delay = Math.min(1000 * 2 ** attempt, 30_000);
      onStatus(`모델 연결을 다시 확인합니다. ${delay / 1000}초 후 자동 재시도합니다.`);
      await new Promise((resolve) => setTimeout(resolve, delay));
    }
  }

  return function loadModels(signal) {
    if (signal?.aborted) return Promise.reject(signal.reason || new DOMException('Aborted', 'AbortError'));
    if (loaded) return Promise.resolve();
    if (!pending) {
      onStatus('모델 연결을 확인하고 있습니다.');
      pending = requestModels().catch((error) => {
        onStatus(error.message);
        throw error;
      }).finally(() => { pending = null; });
    }
    if (!signal) return pending;
    // 한 질문을 중지해도 다른 호출자와 화면 초기화의 복구 작업은 계속한다.
    return new Promise((resolve, reject) => {
      const abort = () => reject(signal.reason || new DOMException('Aborted', 'AbortError'));
      signal.addEventListener('abort', abort, { once: true });
      pending.then(resolve, reject).finally(() => signal.removeEventListener('abort', abort));
    });
  };
}
