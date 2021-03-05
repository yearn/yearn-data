import { VaultV1Contract__factory } from "@contracts/index";
import { Context } from "@data/context";
import { resolveToken } from "@protocols/common/token";
import { objectAll } from "@utils/promise";

import { VaultBase } from "../interfaces";
import { fetchInceptionBlock } from "../reader";

export async function resolveBasic(
  address: string,
  ctx: Context
): Promise<VaultBase> {
  const vault = VaultV1Contract__factory.connect(address, ctx.provider);
  const structure = {
    name: vault.name(),
    symbol: vault.symbol(),
    decimals: vault.decimals(),
    token: vault.token().then((address) => resolveToken(address, ctx)),
    inception: fetchInceptionBlock(address, ctx)
      .then((block) => (block ? block.block : -1))
      .catch(() => -1),
  };
  const result = await objectAll(structure);
  return {
    ...result,
    address,
  };
}
