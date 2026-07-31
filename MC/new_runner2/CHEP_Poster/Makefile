TEX     = pdflatex
TEXOPTS = -interaction=nonstopmode -halt-on-error

poster.pdf: poster.tex
	$(TEX) $(TEXOPTS) poster.tex
	$(TEX) $(TEXOPTS) poster.tex

clean:
	rm -f poster.aux poster.log poster.nav poster.out poster.snm poster.toc poster.vrb

distclean: clean
	rm -f poster.pdf

.PHONY: clean distclean
