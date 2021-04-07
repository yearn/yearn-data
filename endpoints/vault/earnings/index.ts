import fetch from 'node-fetch';
import BigNumber from 'bignumber.js';
import wrap from "../../../utils/wrap";
import { SubGraphUrl } from "../../../settings/env";

const getAccountVaultPositions = async (address) => {
  const query = `
    {
      accountVaultPositions(where: {account: "${address}"}) {
        vault {
          id
        }
        balanceTokens
        updates {
          deposits
          withdrawals
          tokensSent
          tokensReceived
        }
      }
    }
  `;

  const response = await fetch(SubGraphUrl, {
    method: 'POST',
    body: JSON.stringify({ query }),
  });

  const resJson = await response.json();
  return resJson.data;
};

const buildEarningsPerVaultData = (accountVaultPositions) => {
  return accountVaultPositions.map(accountPosition => {
    let { vault: { id }, updates, balanceTokens } = accountPosition;
    updates = convertFieldsToBigNumber(updates, [
      'deposits',
      'withdrawals',
      'tokensSent',
      'tokensReceived'
    ]);

    const {
      deposits,
      withdrawals,
      tokensSent,
      tokensReceived
    } = updates.reduce((totalAmounts, update) => {
      return {
        deposits: totalAmounts.deposits.plus(update.deposits),
        withdrawals: totalAmounts.withdrawals.plus(update.withdrawals),
        tokensSent: totalAmounts.tokensSent.plus(update.tokensSent),
        tokensReceived: totalAmounts.tokensReceived.plus(update.tokensReceived),
      }
    }, {
      deposits: new BigNumber(0),
      withdrawals: new BigNumber(0),
      tokensSent: new BigNumber(0),
      tokensReceived: new BigNumber(0)
    });
    balanceTokens = new BigNumber(balanceTokens);
    
    const totalEarnings = new BigNumber(balanceTokens - deposits + withdrawals - tokensReceived + tokensSent);
    
    return {
      vault: {
        id: id
      },
      totalEarnings: totalEarnings,
      deposits: deposits,
      withdrawals: withdrawals,
      transfersIn: tokensReceived,
      transfersOut: tokensSent
    }
  });
}

function convertFieldsToBigNumber(entities, fields) {
  return entities.map((entity) => {
    fields.forEach((field) => {
      entity[field] = new BigNumber(entity[field]);
    });

    return entity;
  });
}

export const handler = wrap(async (event) => {
  const userAddress = event.pathParameters.userAddress;
  const { accountVaultPositions } = await getAccountVaultPositions(userAddress);
  const earningsPerVaultData = await buildEarningsPerVaultData(accountVaultPositions);

  return earningsPerVaultData;
});
