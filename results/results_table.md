# Evaluation Results

Selected model: **DeBERTa-v3-base (Seed 42)**, exported with τ = 0.97.

## Development Set (Model Selection)

| Architecture     | Char Precision   | Char Recall     | Char F5         | Leak Rate       | Direct Leak Rate   |
|:-----------------|:-----------------|:----------------|:----------------|:----------------|:-------------------|
| DeBERTa-v3-base  | 0.8936 ± 0.0006  | 0.9962 ± 0.0000 | 0.9918 ± 0.0000 | 0.0061 ± 0.0002 | 0.0041 ± 0.0001    |
| ModernBERT-large | 0.9014           | 0.9957          | 0.9917          | 0.0068          | 0.0046             |
| RoBERTa-base     | 0.8942 ± 0.0027  | 0.9958 ± 0.0002 | 0.9915 ± 0.0001 | 0.0061 ± 0.0003 | 0.0039 ± 0.0000    |
| ModernBERT-base  | 0.8977 ± 0.0148  | 0.9955 ± 0.0010 | 0.9913 ± 0.0003 | 0.0072 ± 0.0015 | 0.0044 ± 0.0008    |

## Leak Rate, All Categories (%)

| System                                              |   Nemotron-PII |   ai4privacy OpenPII |   Gretel Finance |   TAB |
|:----------------------------------------------------|---------------:|---------------------:|-----------------:|------:|
| DeBERTa-v3-base                                     |           0.47 |                 0.08 |             2.32 | 13.36 |
| GLiNER-PII                                          |           6.1  |                 3.99 |            25.2  | 21.1  |
| ModernBERT-base                                     |           0.38 |                 0.07 |             2.71 | 14.68 |
| ModernBERT-base (Prototype)                         |           0.34 |                 0.02 |            23.37 | 11.76 |
| ModernBERT-large                                    |           0.33 |                 0    |             2.07 | 13.75 |
| RoBERTa-base                                        |           0.35 |                 0.06 |             2.38 | 13.45 |
| Selected: DeBERTa-v3-base (Seed 42), Argmax         |           1.23 |                 0.39 |             9.71 | 27.31 |
| Selected: DeBERTa-v3-base (Seed 42), Global τ       |           0.41 |                 0.09 |             2.24 | 14.07 |
| Selected: DeBERTa-v3-base (Seed 42), Per-Category τ |           0.72 |                 0.24 |             5.18 | 21.87 |
| spaCy (en_core_web_trf)                             |          57.42 |                54.57 |            41.4  | 14.22 |

## Leak Rate, Direct Identifiers (%)

| System                                              |   Nemotron-PII |   ai4privacy OpenPII |   Gretel Finance |   TAB |
|:----------------------------------------------------|---------------:|---------------------:|-----------------:|------:|
| DeBERTa-v3-base                                     |           0.1  |                 0.09 |             2.24 | 20.41 |
| GLiNER-PII                                          |           8.63 |                 0.75 |            11.9  | 26.57 |
| ModernBERT-base                                     |           0.08 |                 0.09 |             2.53 | 24.69 |
| ModernBERT-base (Prototype)                         |           0.07 |                 0.03 |             9    |  5.9  |
| ModernBERT-large                                    |           0.07 |                 0    |             2.4  | 21.83 |
| RoBERTa-base                                        |           0.09 |                 0.06 |             2.41 | 19.93 |
| Selected: DeBERTa-v3-base (Seed 42), Argmax         |           0.42 |                 0.2  |             8.17 | 35.9  |
| Selected: DeBERTa-v3-base (Seed 42), Global τ       |           0.07 |                 0.08 |             2.01 | 21.43 |
| Selected: DeBERTa-v3-base (Seed 42), Per-Category τ |           0.07 |                 0.08 |             2.13 | 28.33 |
| spaCy (en_core_web_trf)                             |          76.46 |                69.56 |            64.12 | 36.73 |

## Nemotron-PII Test Set

| System                                              | Char Precision   | Char Recall     | Char F5         | Leak Rate       | Direct Leak Rate   | Entity F1       |
|:----------------------------------------------------|:-----------------|:----------------|:----------------|:----------------|:-------------------|:----------------|
| DeBERTa-v3-base                                     | 0.9419 ± 0.0012  | 0.9949 ± 0.0003 | 0.9928 ± 0.0003 | 0.0047 ± 0.0009 | 0.0010 ± 0.0005    | 0.9398 ± 0.0037 |
| ModernBERT-base                                     | 0.9357 ± 0.0111  | 0.9952 ± 0.0003 | 0.9927 ± 0.0002 | 0.0038 ± 0.0008 | 0.0008 ± 0.0002    | 0.9272 ± 0.0119 |
| ModernBERT-large                                    | 0.9460           | 0.9955          | 0.9935          | 0.0033          | 0.0007             | 0.9425          |
| RoBERTa-base                                        | 0.9451 ± 0.0052  | 0.9955 ± 0.0001 | 0.9935 ± 0.0002 | 0.0035 ± 0.0001 | 0.0009 ± 0.0003    | 0.9452 ± 0.0056 |
| ModernBERT-base (Prototype)                         | 0.9239           | 0.9952          | 0.9922          | 0.0034          | 0.0007             | 0.9068          |
| Selected: DeBERTa-v3-base (Seed 42), Argmax         | 0.9868           | 0.9901          | 0.9900          | 0.0123          | 0.0042             | 0.9808          |
| Selected: DeBERTa-v3-base (Seed 42), Global τ       | 0.9428           | 0.9951          | 0.9930          | 0.0041          | 0.0007             | 0.9425          |
| Selected: DeBERTa-v3-base (Seed 42), Per-Category τ | 0.9756           | 0.9927          | 0.9920          | 0.0072          | 0.0007             | 0.9705          |
| spaCy (en_core_web_trf)                             | 0.7772           | 0.3024          | 0.3097          | 0.5742          | 0.7646             | 0.4682          |
| GLiNER-PII                                          | 0.9011           | 0.8102          | 0.8133          | 0.0610          | 0.0863             | 0.7973          |

## ai4privacy OpenPII Test Set

| System                                              | Char Precision   | Char Recall     | Char F5         | Leak Rate       | Direct Leak Rate   | Entity F1       |
|:----------------------------------------------------|:-----------------|:----------------|:----------------|:----------------|:-------------------|:----------------|
| DeBERTa-v3-base                                     | 0.9911 ± 0.0000  | 0.9998 ± 0.0001 | 0.9994 ± 0.0001 | 0.0008 ± 0.0001 | 0.0009 ± 0.0002    | 0.9867 ± 0.0012 |
| ModernBERT-base                                     | 0.9902 ± 0.0018  | 0.9998 ± 0.0000 | 0.9994 ± 0.0000 | 0.0007 ± 0.0000 | 0.0009 ± 0.0002    | 0.9821 ± 0.0015 |
| ModernBERT-large                                    | 0.9939           | 1.0000          | 0.9998          | 0.0000          | 0.0000             | 0.9856          |
| RoBERTa-base                                        | 0.9916 ± 0.0001  | 0.9998 ± 0.0000 | 0.9995 ± 0.0000 | 0.0006 ± 0.0000 | 0.0006 ± 0.0002    | 0.9836 ± 0.0008 |
| ModernBERT-base (Prototype)                         | 0.9861           | 1.0000          | 0.9994          | 0.0002          | 0.0003             | 0.9753          |
| Selected: DeBERTa-v3-base (Seed 42), Argmax         | 0.9971           | 0.9983          | 0.9983          | 0.0039          | 0.0020             | 0.9912          |
| Selected: DeBERTa-v3-base (Seed 42), Global τ       | 0.9911           | 0.9997          | 0.9993          | 0.0009          | 0.0008             | 0.9858          |
| Selected: DeBERTa-v3-base (Seed 42), Per-Category τ | 0.9954           | 0.9989          | 0.9987          | 0.0024          | 0.0008             | 0.9883          |
| spaCy (en_core_web_trf)                             | 0.8205           | 0.5335          | 0.5408          | 0.5457          | 0.6956             | 0.3936          |
| GLiNER-PII                                          | 0.9383           | 0.9572          | 0.9564          | 0.0399          | 0.0075             | 0.5982          |

## Gretel Finance Test Set

| System                                              | Char Precision   | Char Recall     | Char F5         | Leak Rate       | Direct Leak Rate   | Entity F1       |
|:----------------------------------------------------|:-----------------|:----------------|:----------------|:----------------|:-------------------|:----------------|
| DeBERTa-v3-base                                     | 0.6605 ± 0.0015  | 0.9810 ± 0.0012 | 0.9630 ± 0.0010 | 0.0232 ± 0.0012 | 0.0224 ± 0.0031    | 0.6407 ± 0.0039 |
| ModernBERT-base                                     | 0.6770 ± 0.0289  | 0.9790 ± 0.0020 | 0.9624 ± 0.0004 | 0.0271 ± 0.0015 | 0.0253 ± 0.0023    | 0.6355 ± 0.0303 |
| ModernBERT-large                                    | 0.6846           | 0.9839          | 0.9676          | 0.0207          | 0.0240             | 0.6556          |
| RoBERTa-base                                        | 0.6627 ± 0.0041  | 0.9818 ± 0.0009 | 0.9640 ± 0.0005 | 0.0238 ± 0.0011 | 0.0241 ± 0.0027    | 0.6519 ± 0.0005 |
| ModernBERT-base (Prototype)                         | 0.5100           | 0.8351          | 0.8151          | 0.2337          | 0.0900             | 0.3603          |
| Selected: DeBERTa-v3-base (Seed 42), Argmax         | 0.8835           | 0.9275          | 0.9257          | 0.0971          | 0.0817             | 0.8132          |
| Selected: DeBERTa-v3-base (Seed 42), Global τ       | 0.6594           | 0.9818          | 0.9637          | 0.0224          | 0.0201             | 0.6434          |
| Selected: DeBERTa-v3-base (Seed 42), Per-Category τ | 0.7661           | 0.9618          | 0.9525          | 0.0518          | 0.0213             | 0.7363          |
| spaCy (en_core_web_trf)                             | 0.7287           | 0.5691          | 0.5739          | 0.4140          | 0.6412             | 0.4868          |
| GLiNER-PII                                          | 0.6112           | 0.8035          | 0.7939          | 0.2520          | 0.1190             | 0.3224          |

## TAB Test Set

| System                                              | Char Precision   | Char Recall     | Char F5         | Leak Rate       | Direct Leak Rate   | Entity F1       | ER Direct       | ER Quasi        | Word Precision   |
|:----------------------------------------------------|:-----------------|:----------------|:----------------|:----------------|:-------------------|:----------------|:----------------|:----------------|:-----------------|
| DeBERTa-v3-base                                     | 0.6059 ± 0.0097  | 0.8929 ± 0.0057 | 0.8769 ± 0.0061 | 0.1336 ± 0.0099 | 0.2041 ± 0.0144    | 0.4528 ± 0.0088 | 0.9225 ± 0.0670 | 0.8650 ± 0.0047 | 0.6839 ± 0.0087  |
| ModernBERT-base                                     | 0.6252 ± 0.0402  | 0.8955 ± 0.0080 | 0.8807 ± 0.0044 | 0.1468 ± 0.0079 | 0.2469 ± 0.0623    | 0.4518 ± 0.0023 | 0.7205 ± 0.2040 | 0.8638 ± 0.0047 | 0.6884 ± 0.0358  |
| ModernBERT-large                                    | 0.6055           | 0.8917          | 0.8758          | 0.1375          | 0.2183             | 0.4517          | 0.8096          | 0.8663          | 0.6688           |
| RoBERTa-base                                        | 0.6536 ± 0.0075  | 0.9017 ± 0.0024 | 0.8887 ± 0.0017 | 0.1345 ± 0.0034 | 0.1993 ± 0.0066    | 0.4750 ± 0.0055 | 0.9350 ± 0.0359 | 0.8668 ± 0.0011 | 0.7162 ± 0.0060  |
| ModernBERT-base (Prototype)                         | 0.7771           | 0.8882          | 0.8834          | 0.1176          | 0.0590             | 0.4596          | 0.9337          | 0.8961          | 0.7990           |
| Selected: DeBERTa-v3-base (Seed 42), Argmax         | 0.8618           | 0.7425          | 0.7464          | 0.2731          | 0.3590             | 0.5496          | 0.4987          | 0.7794          | 0.8733           |
| Selected: DeBERTa-v3-base (Seed 42), Global τ       | 0.5991           | 0.8888          | 0.8726          | 0.1407          | 0.2143             | 0.4466          | 0.8751          | 0.8617          | 0.6777           |
| Selected: DeBERTa-v3-base (Seed 42), Per-Category τ | 0.7490           | 0.8078          | 0.8053          | 0.2187          | 0.2833             | 0.4841          | 0.5754          | 0.8109          | 0.8015           |
| spaCy (en_core_web_trf)                             | 0.5525           | 0.8935          | 0.8728          | 0.1422          | 0.3673             | 0.4636          | 0.4935          | 0.8827          | 0.5744           |
| GLiNER-PII                                          | 0.7750           | 0.8057          | 0.8045          | 0.2110          | 0.2657             | 0.4970          | 0.6908          | 0.8070          | 0.8192           |

## Leak Rate by Category for the Selected Model (%)

|              |   Nemotron-PII |   ai4privacy OpenPII |   Gretel Finance |    TAB |
|:-------------|---------------:|---------------------:|-----------------:|-------:|
| Person       |           0.11 |                 0.1  |             1.74 |   3.07 |
| Email        |           0.18 |                 0    |             0.56 | nan    |
| Phone        |           0    |                 0    |             0    | nan    |
| Address      |           0    |                 0.26 |             1.89 | nan    |
| Location     |           1.75 |                 0.39 |             0    |  30.5  |
| Date         |           1.11 |                 0    |             1.73 |   3.16 |
| ID Number    |           0    |                 0    |             0    |  68.55 |
| Financial    |           0    |                 0    |             0.59 | nan    |
| Username     |           0    |               nan    |             0    | nan    |
| Credential   |           0.35 |               nan    |             2.97 | nan    |
| IP Address   |           0    |               nan    |            29.85 | nan    |
| URL          |           0    |               nan    |           nan    | nan    |
| Organisation |           0.38 |               nan    |             3.72 |  33.45 |

## Inference Latency (Nemotron-PII Test Set)

| Model                       |   Milliseconds per Document |   Milliseconds per 1,000 Characters |
|:----------------------------|----------------------------:|------------------------------------:|
| RoBERTa-base (Seed 42)      |                         9.1 |                                 9.6 |
| RoBERTa-base (Seed 7)       |                         9.2 |                                 9.7 |
| DeBERTa-v3-base (Seed 42)   |                        20.9 |                                22   |
| DeBERTa-v3-base (Seed 7)    |                        25.4 |                                26.8 |
| ModernBERT-base (Seed 42)   |                        19.8 |                                20.9 |
| ModernBERT-base (Seed 7)    |                        41.6 |                                43.8 |
| ModernBERT-large (Seed 42)  |                        29.5 |                                31.1 |
| ModernBERT-base (Prototype) |                        20   |                                21.1 |
