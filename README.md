# GeoZonalSat

Plugin QGIS 3.x para analise zonal de indices espectrais.

Esta versao entrega um MVP funcional com modo local e busca online Sentinel-2:

- selecao de area vetorial no QGIS;
- carga de Shapefile, GeoPackage, KML e GeoJSON pelo provedor OGR do QGIS;
- uso de feicoes selecionadas;
- dissolucao da area;
- busca online real de Sentinel-2 L2A no catalogo STAC Earth Search/Element 84;
- filtro por area, datas, tolerancia e nuvem maxima;
- download/recorte das bandas online necessarias para os indices escolhidos;
- mascara opcional de nuvem por SCL para as estatisticas;
- leitura de rasters locais de indices ja calculados;
- leitura de bandas brutas locais;
- calculo de NDVI, GNDVI, NDRE, NDMI, SAVI e EVI quando as bandas existem;
- reamostragem/alinhamento de bandas para a grade de referencia;
- recorte por mascara da area de interesse;
- estatisticas zonais reais a partir dos pixels validos;
- tabela dentro do plugin;
- exportacao CSV;
- exportacao XLSX quando Pandas/openpyxl estiverem disponiveis;
- relatorio HTML simples;
- visualizacao do relatorio dentro do proprio plugin no QGIS;
- mapas PNG rapidos organizados por data e indice, com a area centralizada;
- botao `Baixar PNG` em cada mapa do relatorio;
- avisos de erros de calculo, download, carregamento e geracao de mapas;
- classificacao colorida da cobertura de nuvens por cena;
- PDF simples gerado a partir do relatorio HTML quando o Qt do QGIS permitir;
- carregamento dos rasters resultantes no painel do QGIS.

O modo online nao simula imagens. Ele consulta o catalogo real `sentinel-2-l2a`
do Earth Search STAC. Se o catalogo nao retornar cena para a area/data/nuvem,
o plugin informa que nao encontrou imagem.

## Instalacao

1. Copie a pasta `geozonalsat` para a pasta de plugins do QGIS.
2. No QGIS, abra `Complementos > Gerenciar e instalar complementos`.
3. Ative `GeoZonalSat`.
4. Abra o plugin pelo menu ou pelo icone da barra de ferramentas.

Pasta comum no Windows:

```text
C:\Users\<usuario>\AppData\Roaming\QGIS\QGIS3\profiles\default\python\plugins\geozonalsat
```

## Fluxo de uso

1. Em `1. Area`, escolha e valide a camada poligonal.
2. Em `2. Datas e indices`, informe as datas e marque os indices desejados.
3. Em `3. Imagens`, escolha o modo local ou online e selecione as imagens.
4. Em `4. Processar e resultados`, escolha a pasta de saida e inicie o processamento.
5. Em `5. Relatorio`, consulte mapas, graficos, estatisticas e alertas de qualidade.

Por padrao, a aba `Resultados` tambem gera mapas PNG por data e indice. A
configuracao `Margem do mapa` controla quanto contexto aparece ao redor da area
estudada.
Os mapas usam uma visualizacao reamostrada no tamanho final do PNG, para evitar
que o QGIS tente desenhar rasters Sentinel-2 completos em alta resolucao.
Cada processamento grava mapas com um identificador unico, impedindo que o
visualizador interno reutilize um PNG antigo com o mesmo indice e data.
Na legenda dos mapas, marrom/amarelo indica solo ou indice baixo, verde indica
indice alto, e cinza claro indica pixel sem dados ou mascarado.

Ao final do processamento, o plugin abre automaticamente a aba `Relatorio`,
onde o HTML gerado pode ser consultado dentro do proprio QGIS. Tambem existe um
botao opcional para abrir o mesmo arquivo fora do QGIS.
Cada mapa possui um botao `Baixar PNG`. No QGIS, o botao abre a escolha do local
de destino; no navegador externo, o download usa o comportamento do navegador.
Erros parciais nao escondem os resultados corretos: o relatorio mostra os itens
afetados e os mapas que falharam aparecem como cartoes de erro.
O grafico temporal do relatorio mostra uma serie por indice, datas no eixo X,
valores medios no eixo Y, notas de dados ausentes e um grafico separado para
cada area.

## Uso do modo online

1. Na aba `1. Area`, selecione e valide a area de interesse.
2. Na aba `2. Datas e indices`, informe datas especificas ou intervalo.
3. Na aba `3. Imagens`, mude o modo para `Online - Sentinel-2 L2A`.
4. Ajuste a nuvem maxima.
5. Clique em `Testar conexao`, se quiser validar o acesso ao catalogo.
6. Clique em `Buscar imagens disponiveis`.
7. Revise a tabela `Cenas online encontradas` e deixe marcadas as cenas desejadas.
8. Na aba `4. Processar e resultados`, clique em `Iniciar processamento`.

O plugin baixa e recorta apenas as bandas necessarias para os indices escolhidos.
Para Sentinel-2, a opcao `Aplicar mascara automatica de nuvem (SCL) nas
estatisticas` permite excluir nuvem, sombra, cirrus e neve. Ela fica desativada
por padrao para evitar que classificacoes SCL locais eliminem uma area que foi
validada visualmente. Os mapas PNG sempre mostram o indice completo; quando a
opcao estiver ativa, somente as estatisticas usam a mascara.
O percentual exibido como `Nuvem na cena` vem do catalogo e representa toda a
cena Sentinel-2, nao apenas a area estudada. A tabela e o relatorio classificam
esse valor como cobertura baixa, moderada, alta ou muito alta. Cenas acima de
10% recebem destaque de cautela.

## Exemplos de nomes aceitos

Indices prontos:

```text
NDVI_2024-02-13.tif
20240213_NDMI.tif
talhao01_ndre_2024_03_09.tif
```

Bandas brutas:

```text
talhao01_B04_2024-02-13.tif
talhao01_B08_2024-02-13.tif
20240309_B8A.tif
20240309_B11.tif
```

Datas detectadas:

```text
2024-02-13
2024_02_13
20240213
13/02/2024
```

## Bandas esperadas

Sentinel-2:

- `B02`: Blue;
- `B03`: Green;
- `B04`: Red;
- `B05`: Red Edge 1;
- `B08`: NIR;
- `B8A`: Narrow NIR;
- `B11`: SWIR 1.

## Indices

- `NDVI = (B08 - B04) / (B08 + B04)`
- `GNDVI = (B08 - B03) / (B08 + B03)`
- `NDRE = (B08 - B05) / (B08 + B05)`
- `NDMI = (B8A - B11) / (B8A + B11)`
- `SAVI = ((B08 - B04) / (B08 + B04 + L)) * (1 + L)`
- `EVI = 2.5 * ((B08 - B04) / (B08 + 6 * B04 - 7.5 * B02 + 1))`

Para SAVI e EVI, se as bandas parecerem estar escaladas como reflectancia Sentinel
em inteiros, o plugin divide por `10000` antes da formula.

## Saidas

O processamento cria:

```text
GeoZonalSat_Projeto/
  rasters/
    indices calculados
    recortes/
  tabelas/
    estatisticas_completas.csv
    tabela_temporal.csv
  relatorios/
    relatorio_geozonalsat.html
    mapas/
      NDVI/
      NDMI/
      ...
  logs/
    processamento.log
    aoi.gpkg
  cache/
```

## Estatisticas calculadas

Para cada area, data e indice:

- media;
- mediana;
- minimo;
- maximo;
- desvio padrao;
- variancia;
- soma;
- pixels validos;
- pixels NoData;
- percentual valido;
- percentis 5, 25, 75 e 95;
- amplitude;
- area em hectares.

## Arquitetura

- `geozonalsat.py`: entrada do plugin no QGIS.
- `geozonalsat_dialog.py`: interface PyQt5 e orquestracao do `QgsTask`.
- `core/area_manager.py`: validacao e exportacao da area de interesse.
- `core/date_manager.py`: datas explicitas, intervalos e deteccao em nomes.
- `core/local_raster_loader.py`: leitura e classificacao de rasters locais.
- `core/index_calculator.py`: calculo dos indices com GDAL e NumPy.
- `core/zonal_statistics.py`: rasterizacao da area e estatistica zonal real.
- `core/export_manager.py`: CSV, tabela temporal e XLSX opcional.
- `core/report_generator.py`: relatorio HTML.
- `core/map_exporter.py`: mapas PNG organizados por data e indice.
- `core/style_manager.py`: carregamento e simbologia dos rasters no QGIS.
- `core/satellite_providers.py`: base para provedores online reais.
- `core/cloud_mask.py`: base para mascara Sentinel-2 SCL.

## Melhorias futuras

- Implementar Landsat 8/9 Collection 2.
- Baixar bandas sob demanda com cache persistente.
- Exportar GeoPackage com tabela vinculada a geometria.
- Exportar PDF por layout do QGIS ou mecanismo HTML.
- Incluir graficos interativos dentro da janela.
- Criar testes automatizados com rasters pequenos de exemplo.
