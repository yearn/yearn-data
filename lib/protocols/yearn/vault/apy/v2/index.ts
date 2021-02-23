import { Context } from "lib/data/context";
import { Apy } from "lib/protocols/common/apy";
import * as curve from "lib/protocols/curve";

import { VaultV2 } from "../../interfaces";
import { calculateAveraged, shouldBeAveraged } from "./averaged";
import { calculateSimple } from "./simple";

export async function calculate(vault: VaultV2, ctx: Context): Promise<Apy> {
  const isCurveVault = await curve.hasCurvePool(vault.token.address, ctx);
  if (isCurveVault) {
    return await curve.calculateApy(vault, ctx);
  } else if (shouldBeAveraged(vault)) {
    return await calculateAveraged(vault, ctx);
  }
  return await calculateSimple(vault, ctx);
}

export { calculateAveraged, calculateSimple };
