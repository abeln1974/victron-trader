"""
Grid-måler analyse — sideprosjekt.

Problemstilling:
- VM-3P75CT (Victron Modbus): rask (~1s), men mangler L3 på IT-nett (0V)
- Qubino ZMNHXD (Home Assistant): treg (cache ~30s), men måler alle 3 faser

Mål: Finn beste kombinasjon for nøyaktig og rask grid-avlesning.

IT-nett Abelgård: 3-fase 230V IT (isolert nøytral).
L1 og L2 måles av VM-3P75CT. L3 måles kun av Qubino.
"""
import time
import statistics
from datetime import datetime
from victron_modbus import VictronModbus
from ha_qubino import QubinoReader


def sample(v: VictronModbus, q: QubinoReader, n: int = 30, interval: float = 2.0) -> list:
    """Ta N målinger med gitt intervall. Returnerer liste med dict per måling."""
    samples = []
    for i in range(n):
        ts = datetime.now().isoformat(timespec='milliseconds')

        # Victron VM-3P75CT via Modbus (rask, direkte)
        phases = v.get_grid_phases()
        vl1 = phases.get('l1') or 0.0
        vl2 = phases.get('l2') or 0.0

        # Qubino via HA REST (treg, cachet)
        qp = q.get_grid_power()
        if qp:
            ql1 = qp.get('l1') or 0.0
            ql2 = qp.get('l2') or 0.0
            ql3 = qp.get('l3') or 0.0
            q_total = qp.get('total') or 0.0
        else:
            ql1 = ql2 = ql3 = q_total = 0.0

        # Kombinert: Victron L1+L2 (live) + Qubino L3 (siste kjente)
        combined = vl1 + vl2 + ql3

        samples.append({
            'ts': ts, 'i': i+1,
            'vl1': vl1, 'vl2': vl2, 'v_sum': vl1+vl2,
            'ql1': ql1, 'ql2': ql2, 'ql3': ql3, 'q_total': q_total,
            'combined': combined,
        })

        print(
            f"[{i+1:2d}] Vic L1={vl1:+6.0f} L2={vl2:+6.0f} Sum={vl1+vl2:+6.0f}W  |"
            f"  Qub L1={ql1:+5.0f} L2={ql2:+5.0f} L3={ql3:+5.0f} Tot={q_total:+6.0f}W  |"
            f"  Kombinert={combined:+6.0f}W"
        )
        if i < n - 1:
            time.sleep(interval)
    return samples


def analyse(samples: list):
    """Statistisk analyse av måle-avvikene."""
    print("\n" + "="*70)
    print("ANALYSE")
    print("="*70)

    v_sums     = [s['v_sum']    for s in samples]
    q_totals   = [s['q_total']  for s in samples]
    combined   = [s['combined'] for s in samples]
    ql3_vals   = [s['ql3']      for s in samples]

    # Detekter Qubino-frysing: teller unike verdier
    q_unique = len(set(round(x,0) for x in q_totals))
    v_unique = len(set(round(x,0) for x in v_sums))

    print(f"\nAntall målinger:          {len(samples)}")
    print(f"Victron unike verdier:    {v_unique} / {len(samples)}  (høy = responsiv)")
    print(f"Qubino unike verdier:     {q_unique} / {len(samples)}  (lav = treg/cachet)")

    print(f"\n{'':30s} {'Snitt':>8} {'Std':>8} {'Min':>8} {'Max':>8}")
    print("-"*66)
    for label, vals in [
        ("Victron L1+L2 (ingen L3)", v_sums),
        ("Qubino total (alle 3)",    q_totals),
        ("Kombinert V(L1+L2)+Q(L3)", combined),
    ]:
        print(f"  {label:30s} {statistics.mean(vals):+8.1f} {statistics.stdev(vals):8.1f} {min(vals):+8.0f} {max(vals):+8.0f}")

    # L3-bidrag
    l3_mean = statistics.mean(ql3_vals)
    l3_std  = statistics.stdev(ql3_vals) if len(set(ql3_vals)) > 1 else 0
    print(f"\nQubino L3 snitt:   {l3_mean:+.1f}W  std={l3_std:.1f}W")
    print(f"  → L3 er {'STABIL — kan brukes som fast offset' if l3_std < 20 else 'VARIABEL — bør oppdateres jevnlig'}")

    # Avvik mellom Victron og Qubino (uten L3)
    diffs = [s['v_sum'] - (s['ql1']+s['ql2']) for s in samples]
    print(f"\nAvvik Victron vs Qubino (L1+L2): snitt={statistics.mean(diffs):+.1f}W  std={statistics.stdev(diffs):.1f}W")

    # Anbefaling
    print("\n" + "="*70)
    print("ANBEFALING")
    print("="*70)
    if q_unique < len(samples) * 0.3:
        print(f"  Qubino er TREG — oppdaterer kun hvert {len(samples)//max(1,q_unique)*2:.0f}s ca.")
        print(f"  Anbefalt strategi: Victron L1+L2 (live) + Qubino L3 som offset")
        print(f"  L3 offset nå: {l3_mean:+.1f}W  (oppdater hvert 30s fra Qubino)")
    else:
        print("  Qubino er responsiv nok — bruk Qubino total direkte")

    if abs(l3_mean) < 50:
        print(f"  L3-bidrag er lite ({l3_mean:+.1f}W) — Victron-sum alene gir god nok nøyaktighet")
    else:
        print(f"  L3-bidrag er SIGNIFIKANT ({l3_mean:+.1f}W) — må inkluderes for nøyaktighet")

    print()


def continuous_compare(duration_s: int = 120, interval: float = 3.0):
    """Kjør kontinuerlig sammenligning og logg til CSV."""
    import csv, os
    outfile = '/tmp/grid_compare.csv'
    v = VictronModbus()
    v.connect()
    q = QubinoReader()

    print(f"Kjører {duration_s}s sammenligning → {outfile}")
    print(f"{'Tid':12s} {'Vic_sum':>8} {'Q_tot':>8} {'Comb':>8} {'Q_L3':>7} {'Diff':>8}")
    print("-"*60)

    rows = []
    t0 = time.time()
    while time.time() - t0 < duration_s:
        phases = v.get_grid_phases()
        vl1 = phases.get('l1') or 0.0
        vl2 = phases.get('l2') or 0.0
        qp  = q.get_grid_power()
        ql3    = (qp.get('l3') or 0.0) if qp else 0.0
        q_tot  = (qp.get('total') or 0.0) if qp else 0.0
        comb   = vl1 + vl2 + ql3
        diff   = (vl1+vl2) - q_tot

        ts = datetime.now().strftime('%H:%M:%S.%f')[:12]
        print(f"{ts} {vl1+vl2:+8.0f} {q_tot:+8.0f} {comb:+8.0f} {ql3:+7.0f} {diff:+8.0f}")
        rows.append([ts, vl1, vl2, ql3, q_tot, comb])
        time.sleep(interval)

    v.disconnect()
    with open(outfile, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['tid','vl1','vl2','ql3','q_total','combined'])
        w.writerows(rows)
    print(f"\nLagret {len(rows)} rader til {outfile}")
    return rows


if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else 'sample'

    if mode == 'sample':
        print("Modus: 30 raske målinger (2s intervall)")
        v = VictronModbus(); v.connect()
        q = QubinoReader()
        s = sample(v, q, n=30, interval=2.0)
        analyse(s)
        v.disconnect()

    elif mode == 'live':
        secs = int(sys.argv[2]) if len(sys.argv) > 2 else 120
        continuous_compare(duration_s=secs, interval=3.0)

    else:
        print("Bruk: python3 grid_analysis.py [sample|live [sekunder]]")
