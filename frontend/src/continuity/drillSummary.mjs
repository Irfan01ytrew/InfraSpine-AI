// Pure presentation of a drill/simulation comparison (Step G slice 8).
// No I/O, no dates: the same comparison always renders the same summary.

export function summarizeDrillComparison(comparison) {
  const differences = comparison?.differences ?? [];
  const byCode = {};
  for (const difference of differences) {
    byCode[difference.code] = (byCode[difference.code] ?? 0) + 1;
  }
  const missed = byCode.rto_missed_in_drill ?? 0;
  const unrecovered = byCode.affected_not_recovered ?? 0;
  const headline = comparison?.matches
    ? 'Drill matches the simulation forecast.'
    : `${differences.length} difference${differences.length === 1 ? '' : 's'} from the forecast` +
      (missed ? ` · ${missed} RTO miss${missed === 1 ? '' : 'es'}` : '') +
      (unrecovered ? ` · ${unrecovered} resource${unrecovered === 1 ? '' : 's'} not recovered` : '');
  return {
    matches: Boolean(comparison?.matches),
    total: differences.length,
    byCode,
    headline,
    affected: comparison?.affected ?? 0,
    recovered: comparison?.recovered ?? 0,
    simulated: comparison?.simulated ?? 0,
  };
}
