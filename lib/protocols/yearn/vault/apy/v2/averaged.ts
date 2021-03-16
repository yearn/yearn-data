import { VaultV2Contract__factory } from "@contracts/index";
import { Context } from "@data/context";
import { Apy, calculateFromPps } from "@protocols/common/apy";
import {
  createTimedBlock,
  estimateBlockPrecise,
  fetchLatestBlock,
} from "@utils/block";
import { seconds } from "@utils/time";
import semver from "semver";

import { VaultV2 } from "../../interfaces";
import { fetchHarvestCalls } from "../../reader";

const AveragedFromVersion = "0.3.2";
const MinHarvests = 10;

export function shouldBeAveraged(vault: VaultV2): boolean {
  return semver.gte(vault.apiVersion, AveragedFromVersion);
}

export async function calculateAveragedApy(
  vault: VaultV2,
  ctx: Context
): Promise<Apy> {
  const contract = VaultV2Contract__factory.connect(
    vault.address,
    ctx.provider
  );
  const harvests = await fetchHarvestCalls(vault, ctx);
  if (harvests.length < MinHarvests) {
    return {
      recommended: 0,
      composite: false,
      type: "error",
      description: "no harvests",
      data: {
        oneWeekSample: null,
        oneMonthSample: null,
        inceptionSample: null,
        grossApy: null,
        netApy: null,
        performanceFee: null,
        managementFee: null,
      },
    };
  }

  const latest = await fetchLatestBlock(ctx);
  const inception = await createTimedBlock(harvests[2], ctx);
  const oneWeek = await estimateBlockPrecise(
    latest.timestamp - seconds("1 week"),
    ctx
  );
  const oneMonth = await estimateBlockPrecise(
    latest.timestamp - seconds("4 weeks"),
    ctx
  );
  const ppsSampleData = await calculateFromPps(
    latest.block,
    inception.block,
    {
      oneWeekSample: oneWeek,
      oneMonthSample: oneMonth,
      inceptionSample: inception.block,
    },
    contract.pricePerShare
  );

  const netApy = Math.max(
    ppsSampleData.oneMonthSample || 0,
    ppsSampleData.oneWeekSample || 0
  );
  const v2PerformanceFee = vault.fees.general.performanceFee / 1e4;
  const v2ManagementFee = vault.fees.general.managementFee / 1e4;
  const grossApy = netApy / (1 - v2PerformanceFee) + v2ManagementFee;

  const data = {
    oneMonthSample: ppsSampleData.oneMonthSample || 0,
    oneWeekSample: ppsSampleData.oneWeekSample || 0,
    grossApy,
    netApy,
    performanceFee: v2PerformanceFee,
    managementFee: v2ManagementFee,
  };

  const apy = {
    recommended: netApy || 0,
    type: "pricePerShareV2OneMonth",
    composite: false,
    description: "Price per share - One month sample",
    data,
  };

  return apy;
}
