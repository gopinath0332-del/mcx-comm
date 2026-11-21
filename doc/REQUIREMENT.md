i want to create strategy based on gold.pine pinescript. This project is about mcx commodity trade. Currenlty application trade on Mcx gold petal . In future, i would like to change from gold petal to gold guinea/ gold mini / other gold contract

currently i am trading in Zerodha and zerodha authentication logic is handled in the D:\Workspace\nifty-strike-engine project. applicaiton can reuse toke created from this project. (no need to dupolicate login logic again)

Currently i am starting with GOLD, in future i may add crudeoil / silver/ naturalgas with different strategy . so keep this project future proof and adaptable.

Time interval is 1 HR
Candle type is Heinkin Ashi candle

Gold will be started with fixed capitl of 200000. This project trades in Futures. meaning GOLDPETAL Futures.

Base Rules for selecting future :

1. Application should select next month future for strategy
2. If position is opened for gold in exchange, application should use that month future data to track exit signal.

All the entry signal/exit signal / buy selll sigannl. and all communication should be sent to discord webhook.

Also applicaiton should decide the nummber of lots to purchase. lot size calcuation should be based on details from image.png ( lot calcuation is handled in D:\Workspace\delta-exchange-journal (please ignore "Entry Price (₹) optional" field))

Analyse the gold.pine and create application.
