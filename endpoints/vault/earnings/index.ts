import fetch from "node-fetch";
import BigNumber from "bignumber.js";
import wrap from "../../../utils/wrap";
import { SubGraphUrl } from "../../../settings/env";

interface RepsonseContainer {
  data: AccountContainer;
}

interface AccountContainer {
  account: AccountEvents;
}

interface AccountEvents {
  deposits: VaultEvent[];
  tokensReceived: VaultEvent[];
  tokensSent: VaultEvent[];
  withdrawals: VaultEvent[];
  vaultPositions: VaultEvent[];
}

interface VaultEvent {
  tokenAmount: string;
  vault: VaultIdContainer;
}

interface VaultIdContainer {
  id: string;
}

interface Event {
  pathParameters: PathParameters;
}

interface PathParameters {
  userAddress: string;
}

const getAccountEarnings = async (address: string) => {
  const query = `
  {
    account(id: "${address}") {
      tokensSent: sharesSent {
        tokenAmount
        vault {
          id
        }
      }
      tokensReceived: sharesReceived {
        tokenAmount
        vault {
          id
        }
      }
      deposits {
        tokenAmount
        vault {
          id
        }
      }
      withdrawals {
        tokenAmount
        vault {
          id
        }
      }
      vaultPositions {
        tokenAmount: balanceTokens
        vault {
          id
        }
      }    
    }
  }
  `;

  const response = await fetch(SubGraphUrl || "", {
    method: "POST",
    body: JSON.stringify({ query }),
  });

  const container: RepsonseContainer = await response.json();

  const addresses = findAllVaultAddresses(container.data.account);

  const result = addresses.map((address: String) => {
    const earnings = calculateEarningsForVaultAddress(
      address,
      container.data.account
    );
    return {
      vaultAddress: address,
      earnings: earnings,
    };
  });

  return result;
};

function findAllVaultAddresses(accountEvents: AccountEvents) {
  const events = [
    ...accountEvents.deposits,
    ...accountEvents.tokensReceived,
    ...accountEvents.tokensSent,
    ...accountEvents.withdrawals,
    ...accountEvents.vaultPositions,
  ];

  const vaultAddresses = events.map((event) => event.vault.id);
  const set = new Set(vaultAddresses);
  return Array.from(set);
}

function calculateEarningsForVaultAddress(
  vaultAddress: string,
  accountEvents: AccountEvents
) {
  const deposits = sumTokensUsedForEvents(accountEvents.deposits, vaultAddress);
  const sharesReceived = sumTokensUsedForEvents(
    accountEvents.tokensReceived,
    vaultAddress
  );
  const sharesSent = sumTokensUsedForEvents(
    accountEvents.tokensSent,
    vaultAddress
  );
  const withdrawals = sumTokensUsedForEvents(
    accountEvents.withdrawals,
    vaultAddress
  );
  const vaultPositions = sumTokensUsedForEvents(
    accountEvents.vaultPositions,
    vaultAddress
  );

  let positiveValues = vaultPositions.plus(withdrawals).plus(sharesSent)
  let negativeValues = deposits.plus(sharesReceived)

  if (positiveValues.isGreaterThan(negativeValues)) {
    return positiveValues
    .minus(negativeValues)
    .div(10 ** 18)
    .toNumber();
  } else {
    // likedly completely withdrawn and erronously slightly negative
    return 0
  } 
}

function sumTokensUsedForEvents(events: VaultEvent[], vaultAddress: string) {
  const bigZero = new BigNumber(0);

  return events
    .filter((event) => {
      return event.vault.id === vaultAddress;
    })
    .map((event) => {
      return new BigNumber(event.tokenAmount);
    })
    .reduce((total, amount) => total.plus(amount), bigZero);
}

export const handler = wrap(async (event: Event) => {
  const userAddress = event.pathParameters.userAddress;
  return await getAccountEarnings(userAddress);
});
