#The place where all the replicas logic will be saved.
from VR_replica import replica
import asyncio

#TODO: Add function to check where a player will endup given the gamestate and intended movemnts of all players
#TODO: Add function for logic in player movement


def main():
    rep = replica.replica("192.168.0.10")
    


if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    loop.run_until_complete(asyncio.wait(main()))
    # asyncio.run(main())
