/**
 * Cumulative normal distribution.
 *
 * Hart (1968) rational approximation, in the form popularised by West (2005),
 * "Better Approximations to Cumulative Normal Functions".
 *
 * WHY NOT Abramowitz & Stegun 7.1.26: A&S carries ~1.5e-7 ABSOLUTE error.
 * For a deep-OTM option whose N(d2) term is itself ~1e-6, that absolute error
 * becomes a percent-level RELATIVE error on the premium, which then propagates
 * into IV inversion and every Greek. Hart is accurate to ~1e-15 absolute across
 * the whole double-precision range, so the approximation is never the limiting
 * factor. This is not a micro-optimisation; it is a correctness requirement.
 *
 * PURE MODULE: no imports, no I/O, no clock, no config.
 */

/** Cumulative standard normal, N(x) = P(Z <= x). */
export function normCdf(x: number): number {
  if (Number.isNaN(x)) return NaN;
  if (x < 0) return 1 - normCdf(-x);

  const z = x;
  if (z > 37) return 1;

  const e = Math.exp((-z * z) / 2);
  let v: number;

  if (z < 7.07106781186547) {
    let b = 3.52624965998911e-2 * z + 0.700383064443688;
    b = b * z + 6.37396220353165;
    b = b * z + 33.912866078383;
    b = b * z + 112.079291497871;
    b = b * z + 221.213596169931;
    b = b * z + 220.206867912376;

    let c = 8.83883476483184e-2 * z + 1.75566716318264;
    c = c * z + 16.064177579207;
    c = c * z + 86.7807322029461;
    c = c * z + 296.564248779674;
    c = c * z + 637.333633378831;
    c = c * z + 793.826512519948;
    c = c * z + 440.413735824752;

    v = (e * b) / c;
  } else {
    let b = z + 0.65;
    b = z + 4 / b;
    b = z + 3 / b;
    b = z + 2 / b;
    b = z + 1 / b;
    v = e / (b * 2.506628274631);
  }

  return 1 - v;
}

/** Standard normal probability density. */
export function normPdf(x: number): number {
  return Math.exp((-x * x) / 2) / Math.sqrt(2 * Math.PI);
}
