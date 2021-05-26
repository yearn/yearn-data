import { Erc20Contract__factory } from "@contracts/index";
import { Context } from "@data/context";
import { objectAll } from "@utils/promise";

export interface Token {
  symbol: string;
  address: string;
  decimals: number;
  name: string;
}

export async function resolveToken(address: string, ctx: Context): Promise<Token> {
  const token = Erc20Contract__factory.connect(address, ctx.provider);
  const structure = {
    name: token.name(),
    symbol: token.symbol(),
    decimals: token.decimals(),
  };
  const result = await objectAll(structure);
  return {
    ...result,
    address,
  };
}
