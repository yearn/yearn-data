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

const oracles = Object.keys(pairs);
const graph: Record<string, Record<string, string[]>> = ((pairs) => {
  const graph = {};
  for (const oracle of oracles) {
    const subgraph = {};
    for (const [t0, t1] of pairs[oracle]) {
      if (!subgraph[t0]) subgraph[t0] = [];
      if (!subgraph[t1]) subgraph[t1] = [];
      subgraph[t0].push(t1);
      subgraph[t1].push(t0);
    }
    graph[oracle] = subgraph;
  }
  return graph;
})(pairs);

function shortestPath(
  graph: Record<string, string[]>,
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
      const newpath = shortestPath(graph, node, end, [...path]);
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
  if (alias) return supported(alias);
  return Object.values(graph)
    .map((a) => Object.keys(a))
    .reduce((a, b) => a.concat(b))
    .includes(token);
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
  const alias = aliases[start];
  if (alias) start = alias;

  const paths = oracles.map((oracle) => ({
    oracle,
    path: shortestPath(graph[oracle], start, end),
  }));

  const short = paths.reduce((prev, next) => {
    if (prev.path === null) return next;
    else if (next.path === null) return prev;
    return prev.path.length > next.path.length ? next : prev;
  });

  if (!short.path) {
    throw new Error("no direct path from start to end");
  }

  const oracle = UniquoteOracle__factory.connect(short.oracle, ctx.provider);

  for (const [token0, token1] of pairwise(short.path)) {
    amount = toBigNumber(
      await oracle.current(token0, amount.toString(), token1)
    );
  }

  return amount;
}
