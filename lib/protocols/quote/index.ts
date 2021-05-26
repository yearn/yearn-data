import { Quote__factory } from "@contracts/index";
import { Context } from "@data/context";
import { toBigNumber } from "@utils/bignumber";
import BigNumber from "bignumber.js";

import aliases from "./aliases.json";

export const QuoteAddress = "0x83d95e0D5f402511dB06817Aff3f9eA88224B030";

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
  if (start === end && end === USDC.address)
    return toBigNumber(10 ** USDC.decimals);
  const quote = Quote__factory.connect(QuoteAddress, ctx.provider);
  if (end === USDC.address) {
    return quote.getPriceUsdcRecommended(start).then(toBigNumber);
  }
  return quote.getPriceFromRouter(start, end).then(toBigNumber);
}
