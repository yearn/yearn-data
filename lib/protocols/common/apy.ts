import { BigNumber, toBigNumber } from "@utils/bignumber";
import { Block } from "@utils/block";
import { BlocksPerDay } from "@utils/constants";
import { CallOverrides, ethers } from "ethers";
import fromEntries from "fromentries";

export interface Apy {
  recommended: number | string;
  composite: boolean;
  type: string;
  description: string;
  data?: Record<string, unknown>;
}

export interface Snapshot {
  value: BigNumber;
  block: Block;
}

export type ApyBlocks = {
  [name: string]: Block; // block #
};

export type ApyValues<T extends ApyBlocks> = {
  [A in keyof T]: number | null; // apy (0-1 : 0-100%), or null
};

export type ApyType = {
  type: string;
  description: string;
};

export type ApyEntry = [string, number | null];

export function calculateYearlyRoi(
  current: Snapshot,
  previous: Snapshot,
  blocksPerDay = BlocksPerDay
): number {
  const valueDelta = current.value
    .minus(previous.value)
    .dividedBy(previous.value);
  const blockDelta = new BigNumber(current.block - previous.block);
  const derivative = valueDelta.div(blockDelta);
  return derivative.toNumber() * blocksPerDay * 365;
}

export async function calculateFromPps<T extends ApyBlocks>(
  referenceBlock: number,
  inceptionBlock: number,
  snapshotsBlocks: T,
  pricePerShare: (options?: CallOverrides) => Promise<ethers.BigNumber>
): Promise<ApyValues<T>> {
  const snaps = Object.entries(snapshotsBlocks).sort((a, b) => b[1] - a[1]);
  if (snaps.length === 0) {
    return {} as ApyValues<T>;
  }

  const reference: Snapshot = {
    block: referenceBlock,
    value: toBigNumber(await pricePerShare({ blockTag: referenceBlock })),
  };

  const cache: number[] = [];
  const calculated = await snaps
    .map(([name, block]) => async (entries: ApyEntry[]) => {
      if (block < inceptionBlock || block > referenceBlock) {
        entries.push([name, cache.length ? cache[cache.length - 1] : null]);
        return entries;
      }
      const snapshot: Snapshot = {
        block,
        value: toBigNumber(await pricePerShare({ blockTag: block })),
      };
      const apy = calculateYearlyRoi(reference, snapshot);
      cache.push(apy);
      entries.push([name, apy]);
      return entries;
    })
    .reduce((promise, fn) => promise.then(fn), Promise.resolve<ApyEntry[]>([]));

  return fromEntries(calculated) as ApyValues<T>;
}
