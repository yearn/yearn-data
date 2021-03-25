import { Quote__factory } from "@contracts/index";
import { Context } from "@data/context";
import { toBigNumber } from "@utils/bignumber";
import BigNumber from "bignumber.js";

import aliases from "./aliases.json";

export const QuoteAddress = "0x9b8b9F6146B29CC32208f42b995E70F0Eb2807F3";

export const USDC = {
  address: "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
  decimals: 6,
};

export function aliased(token: string): string {
  const alias = aliases[token];
  if (alias) {
    return alias;
  }
  return token;
}

export async function price(
  start: string,
  end: string,
  ctx: Context
): Promise<BigNumber> {
  start = aliased(start);
  end = aliased(end);
  if (start === end && end == USDC.address)
    return toBigNumber(10 ** USDC.decimals);
  const quote = Quote__factory.connect(QuoteAddress, ctx.provider);
  return quote.getPriceFromRouter(start, end).then(toBigNumber);
}
