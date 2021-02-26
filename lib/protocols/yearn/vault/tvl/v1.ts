import { VaultV1Contract__factory } from "@contracts/index";
import { Context } from "@data/context";
import { toBigNumber } from "@utils/bignumber";

import { VaultV1 } from "../interfaces";

export async function calculateTvl(
  vault: VaultV1,
  ctx: Context
): Promise<number> {
  const contract = VaultV1Contract__factory.connect(
    vault.address,
    ctx.provider
  );
  return contract.balance().then((num) =>
    toBigNumber(num)
      .div(10 ** vault.decimals)
      .toNumber()
  );
}
