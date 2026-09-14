#!/usr/bin/env python3
"""Turn the UART trace into a readable table."""
import sys
log = open(sys.argv[1]).read().splitlines()
data = [l.split(',') for l in log if l.startswith('#DATA')]
cfg  = {f[1]: f[2] for f in (l.split(',') for l in log if l.startswith('#CFG'))}
prog = cfg.get('program', 'bench')

if prog == 'gate':
    ok = sum(1 for f in data if f[-1] == '1')
    print(f"  SSM inference on CV32E40X: d_model={cfg.get('d_model')} "
          f"d_state={cfg.get('d_state')} layers={cfg.get('layers')} timesteps={cfg.get('timesteps')}")
    for f in data:
        print(f"    beat {f[1]}: expect={f[3]} got={f[4]} {'OK' if f[-1]=='1' else 'MISMATCH'}")

    # The firmware reports its own tally over the UART. Trust that, not the rows
    # that survived the decode: a dropped line would otherwise shrink the
    # denominator and still print PASS. Cross-check both, and refuse to pass on
    # a short read.
    res = next((l.split(',') for l in log if l.startswith('#RESULT,exact')), None)
    fw_ok, fw_tot = (int(res[2]), int(res[4])) if res else (None, None)
    beats = int(cfg['beats']) if 'beats' in cfg else None
    complaints = []
    if beats is not None and len(data) != beats:
        complaints.append(f"decoded {len(data)} beat rows but #CFG,beats={beats}"
                          " -- UART output was truncated")
    if fw_tot is None:
        complaints.append("no #RESULT line from the firmware -- run did not finish")
    elif (ok, len(data)) != (fw_ok, fw_tot):
        complaints.append(f"summary counts {ok}/{len(data)} but the firmware"
                          f" reported {fw_ok}/{fw_tot}")

    total = fw_tot if fw_tot is not None else len(data)
    passed = not complaints and ok == total
    print(f"\n  RESULT: {ok}/{total} exact vs golden reference"
          f" -> {'PASS' if passed else 'FAIL'}")
    for c in complaints:
        print(f"  !! {c}")
    if not passed:
        sys.exit(1)
else:
    rows = {}
    for f in data:
        k = rows.setdefault(f[1], {'cyc': int(f[3]), 'ins': int(f[4])})
        k[f[6]] = int(f[7])
    print(f"  {'kernel':14s}{'cycles':>8}{'instret':>9}{'IPC':>7}{'br_taken':>10}{'ld_stall':>9}")
    for k, v in rows.items():
        if v['cyc'] == 0: continue
        print(f"  {k:14s}{v['cyc']:>8}{v['ins']:>9}{v['ins']/v['cyc']:>7.3f}"
              f"{v.get('branch_tkn',0):>10}{v.get('ld_stall',0):>9}")
    s = rows.get('ssm_scan_q15')
    if s:
        print(f"\n  ssm_scan_q15 is the SSM recurrence (32 timesteps x 16 state = 512 elements)")
        print(f"    {s['ins']/512:.1f} instructions and {s['cyc']/512:.1f} cycles per element")
        print(f"    {s['cyc']-s['ins']} stall cycles vs {s.get('branch_tkn',0)} taken branches"
              f"  ->  branch-bound")
        print(f"    ld_stall={s.get('ld_stall',0)}  wb_data_stall={s.get('wb_data_stall',0)}"
              f"  ->  memory keeps up")
