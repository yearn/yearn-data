import { VaultV2Contract__factory } from "@contracts/index";
import { Context } from "@data/context";
import { toBigNumber } from "@utils/bignumber";
import BigNumber from "bignumber.js";

import { VaultV2 } from "../interfaces";

export async function calculateTotalAssets(vault: VaultV2, ctx: Context): Promise<BigNumber> {
  const contract = VaultV2Contract__factory.connect(vault.address, ctx.provider);
  return contract.totalAssets().then((num) => toBigNumber(num));
}
