import { useEffect, useState } from 'react';
import { api, ApiError } from '@/lib/api';

// Internal-only feature: the endpoint 403s for non-internal accounts. After
// the first 403 we stop asking for the rest of the session so regular alpha
// users get zero polling noise and no trace of the feature.
let internalLtpDenied = false;

export interface LtpData {
  symbol: string;
  ltp: number | null;
  prev_close: number | null;
  source: 'angelone_live' | 'eod_close' | string;
  asof: string | null;
}

/** Poll the internal LTP endpoint (5s) while a symbol is shown. Null when
 *  unavailable or when the account is not on the internal allowlist. */
export function useInternalLtp(symbol: string | undefined) {
  const [data, setData] = useState<LtpData | null>(null);

  useEffect(() => {
    setData(null);
    if (!symbol || internalLtpDenied) return;
    let cancelled = false;

    const fetchOnce = () => {
      api
        .getInternalLtp(symbol)
        .then((d) => {
          if (!cancelled) setData(d as LtpData);
        })
        .catch((e) => {
          if (e instanceof ApiError && e.status === 403) internalLtpDenied = true;
          if (!cancelled) setData(null);
        });
    };

    fetchOnce();
    const t = setInterval(fetchOnce, 5000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [symbol]);

  return data;
}
