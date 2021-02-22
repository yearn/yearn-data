import { VaultV2Contract__factory } from "lib/contracts/index";
import { Context } from "lib/data/context";
import { toBigNumber } from "lib/utils/bignumber";

import { VaultV2 } from "../interfaces";

export async function calculateTvlV2(
  vault: VaultV2,
  ctx: Context
): Promise<number> {
  const contract = VaultV2Contract__factory.connect(
    vault.address,
    ctx.provider
  );
  return contract.totalAssets().then((num) =>
    toBigNumber(num)
      .div(10 ** vault.decimals)
      .toNumber()
  );
}
