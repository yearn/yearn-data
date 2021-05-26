import { VaultV1Contract__factory } from "@contracts/index";
import { Context } from "@data/context";
import { toBigNumber } from "@utils/bignumber";
import BigNumber from "bignumber.js";

import { VaultV1 } from "../interfaces";

export async function calculateTotalAssets(vault: VaultV1, ctx: Context): Promise<BigNumber> {
  const contract = VaultV1Contract__factory.connect(vault.address, ctx.provider);
  return contract.balance().then((num) => toBigNumber(num));
}
