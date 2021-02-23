import { Context } from "lib/data/context";
import { Apy } from "lib/protocols/common/apy";
import * as curve from "lib/protocols/curve";

import { VaultV1 } from "../../interfaces";
import { calculateSimple } from "./simple";

export async function calculate(vault: VaultV1, ctx: Context): Promise<Apy> {
  const isCurveVault = await curve.hasCurvePool(vault.token.address, ctx);

  if (isCurveVault) {
    return await curve.calculateApy(vault, ctx);
  }
  return await calculateSimple(vault, ctx);
}

export { calculateSimple };
