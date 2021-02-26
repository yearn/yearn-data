import { UniquoteOracle__factory } from "@contracts/index";
import { Context } from "@data/context";
import { toBigNumber } from "@utils/bignumber";
import BigNumber from "bignumber.js";

import aliases from "./aliases.json";
import pairs from "./pairs.json";

export const SushiOracleAddress = "0xf67Ab1c914deE06Ba0F264031885Ea7B276a7cDa";

export const USDC = {
  address: "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
  decimals: 6,
};

const graph: Record<string, string[]> = ((pairs) => {
  const graph = {};
  for (const [t0, t1] of pairs) {
    if (!graph[t0]) graph[t0] = [];
    if (!graph[t1]) graph[t1] = [];
    graph[t0].push(t1);
    graph[t1].push(t0);
  }
  return graph;
})(pairs);

function shortestPath(
  start: string,
  end: string,
  path: string[] = []
): string[] | null {
  path.push(start);
  if (start.toLowerCase() === end.toLowerCase()) return path;
  if (!Object.keys(graph).includes(start)) return null;
  let shortest: string[] | null = null;
  for (const node of graph[start]) {
    if (!path.includes(node)) {
      const newpath = shortestPath(node, end, [...path]);
      if (newpath) {
        if (!shortest || newpath.length < shortest.length) {
          shortest = newpath;
        }
      }
    }
  }
  return shortest;
}

function pairwise<T>(arr: T[]): [T, T][] {
  const result: [T, T][] = [];
  arr.reduce((prev, current) => {
    result.push([prev, current]);
    return current;
  });
  return result;
}

export function supported(token: string): boolean {
  const alias = aliases[token];
  if (alias) {
    return Object.keys(graph).includes(alias);
  }
  return Object.keys(graph).includes(token);
}

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
  amount: BigNumber,
  ctx: Context
): Promise<BigNumber> {
  const oracle = UniquoteOracle__factory.connect(
    SushiOracleAddress,
    ctx.provider
  );

  const alias = aliases[start];
  if (alias) start = alias;

  const jumps = shortestPath(start, end);
  if (!jumps) {
    throw new Error("no direct path from start to end");
  }

  for (const [token0, token1] of pairwise(jumps)) {
    amount = toBigNumber(
      await oracle.current(token0, amount.toString(), token1)
    );
  }

  return amount;
}
