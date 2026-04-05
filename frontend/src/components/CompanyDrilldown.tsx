import { useEffect, useState } from 'react';
import { ImpactReport, CompanyProfile } from '@/types';
import { api } from '@/lib/api';
import { PieChart, Pie, Cell, Tooltip, ResponsiveContainer } from 'recharts';

const COLORS = ['#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#ec4899'];

interface Props {
  selectedReport: ImpactReport | null;
}

export function CompanyDrilldown({ selectedReport }: Props) {
  const [profile, setProfile] = useState<CompanyProfile | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const primarySymbol = selectedReport?.company_impacts.find(
    (i) => i.hop_distance === 0
  )?.symbol;

  useEffect(() => {
    if (!primarySymbol) return;
    setLoading(true);
    setError(null);

    api
      .getCompany(primarySymbol)
      .then(setProfile)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, [primarySymbol]);

  if (!selectedReport) {
    return (
      <div className="flex items-center justify-center h-full text-gray-600 text-sm">
        Select a news item to drill down
      </div>
    );
  }

  if (loading) {
    return <div className="p-4 text-gray-400 text-sm animate-pulse">Loading company data...</div>;
  }

  if (error) {
    return <div className="p-4 text-red-400 text-sm">Error: {error}</div>;
  }

  if (!profile) return null;

  const segments = profile.business.revenue_segments || [];

  return (
    <div className="flex flex-col h-full bg-gray-900 border border-gray-700 rounded overflow-auto">
      <div className="px-3 py-2 border-b border-gray-700">
        <span className="font-mono font-bold text-white">{profile.identity.company_name}</span>
        <span className="ml-2 text-xs text-gray-500 font-mono">{profile.identity.symbol}</span>
        <span className="ml-2 text-xs text-blue-400">{profile.identity.sector_nse}</span>
      </div>

      <div className="p-3 space-y-4">
        {/* Key Metrics */}
        <div className="grid grid-cols-3 gap-2">
          {[
            { label: 'P/E', value: profile.financials.pe_ratio?.toFixed(1) },
            { label: 'EBITDA%', value: profile.financials.ebitda_margin_pct != null ? profile.financials.ebitda_margin_pct.toFixed(1) + '%' : undefined },
            { label: 'EPS TTM', value: profile.financials.eps_ttm != null ? '₹' + profile.financials.eps_ttm.toFixed(0) : undefined },
          ].map(({ label, value }) => (
            <div key={label} className="bg-gray-800 rounded p-2 text-center">
              <div className="text-xs text-gray-500">{label}</div>
              <div className="text-sm font-mono font-bold text-white">{value ?? '—'}</div>
            </div>
          ))}
        </div>

        {/* DCF Summary */}
        <div className="bg-gray-800 rounded p-3">
          <div className="text-xs text-gray-400 mb-2 uppercase tracking-wider">DCF Model</div>
          <div className="grid grid-cols-3 gap-2 text-center">
            <div>
              <div className="text-xs text-red-400">Bear</div>
              <div className="text-sm font-mono">₹{profile.dcf_model.bear_value?.toFixed(0) ?? '—'}</div>
            </div>
            <div className="border-x border-gray-700">
              <div className="text-xs text-blue-400">Fair Value</div>
              <div className="text-sm font-mono font-bold text-white">
                ₹{profile.dcf_model.fair_value_per_share?.toFixed(0) ?? '—'}
              </div>
            </div>
            <div>
              <div className="text-xs text-green-400">Bull</div>
              <div className="text-sm font-mono">₹{profile.dcf_model.bull_value?.toFixed(0) ?? '—'}</div>
            </div>
          </div>
          <div className="mt-2 text-center text-xs text-gray-500">
            MoS: {profile.dcf_model.margin_of_safety_pct?.toFixed(1) ?? '—'}% | WACC:{' '}
            {profile.dcf_model.wacc_pct?.toFixed(1) ?? '—'}%
          </div>
        </div>

        {/* Revenue Segments */}
        {segments.length > 0 && (
          <div>
            <div className="text-xs text-gray-400 mb-2 uppercase tracking-wider">Revenue Segments</div>
            <div className="flex items-center gap-4">
              <ResponsiveContainer width={120} height={120}>
                <PieChart>
                  <Pie data={segments} dataKey="pct_of_revenue" cx="50%" cy="50%" outerRadius={55}>
                    {segments.map((_, i) => (
                      <Cell key={i} fill={COLORS[i % COLORS.length]} />
                    ))}
                  </Pie>
                  <Tooltip formatter={(v: number) => `${v.toFixed(1)}%`} />
                </PieChart>
              </ResponsiveContainer>
              <div className="flex-1 space-y-1">
                {segments.map((seg, i) => (
                  <div key={seg.name} className="flex items-center gap-2 text-xs">
                    <span
                      className="w-2 h-2 rounded-sm flex-shrink-0"
                      style={{ background: COLORS[i % COLORS.length] }}
                    />
                    <span className="text-gray-300 truncate flex-1">{seg.name}</span>
                    <span className="text-gray-500 font-mono">{seg.pct_of_revenue.toFixed(0)}%</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}

        {/* Analyst Consensus */}
        <div className="bg-gray-800 rounded p-3">
          <div className="text-xs text-gray-400 mb-2 uppercase tracking-wider">Analyst Consensus</div>
          <div className="flex gap-1 h-2 rounded overflow-hidden">
            <div className="bg-green-500" style={{ width: `${profile.analyst_consensus.buy_pct ?? 0}%` }} />
            <div className="bg-yellow-500" style={{ width: `${profile.analyst_consensus.hold_pct ?? 0}%` }} />
            <div className="bg-red-500" style={{ width: `${profile.analyst_consensus.sell_pct ?? 0}%` }} />
          </div>
          <div className="flex justify-between text-xs mt-1">
            <span className="text-green-400">Buy {profile.analyst_consensus.buy_pct?.toFixed(0)}%</span>
            <span className="text-yellow-400">Hold {profile.analyst_consensus.hold_pct?.toFixed(0)}%</span>
            <span className="text-red-400">Sell {profile.analyst_consensus.sell_pct?.toFixed(0)}%</span>
          </div>
          <div className="mt-1 text-center text-xs text-gray-500">
            Target: ₹{profile.analyst_consensus.median_target_price?.toFixed(0) ?? '—'}
          </div>
        </div>
      </div>
    </div>
  );
}
